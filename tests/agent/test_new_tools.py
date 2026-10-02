"""ask_user, task (sub-agents) and the transcript viewer's items."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from custom_console.agent.context import ContextManager
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.permissions import PermissionLevel
from custom_console.agent.questions import Answer, Choice
from custom_console.agent.results import ToolResult
from custom_console.agent.session import AgentSession
from custom_console.agent.subagent import EXCLUDED_TOOLS, SubAgents, subagent_tools
from custom_console.agent.tools import build_tools
from custom_console.agent.tools.ask import ask_tools, parse_options
from custom_console.agent.tools.base import guarded, zone_level
from custom_console.agent.tools.task import task_tools
from custom_console.agent.transcript import build_items
from custom_console.agent.turn import TurnStats, TurnView
from custom_console.agent.usage import UsageLedger


def tool_named(tools, name):
    return next(tool for tool in tools if tool.__name__ == name)


# --------------------------------------------------------------------------- #
# ask_user
# --------------------------------------------------------------------------- #


class TestAskUser:
    def ask(self, make_ctx, answer, **arguments):
        ctx, _ = make_ctx()
        asked = []

        def ask_user(question, choices, multiple, allow_other):
            asked.append((question, choices, multiple, allow_other))
            return answer

        ctx.ask_user = ask_user
        result = tool_named(ask_tools(ctx), "ask_user")(**arguments)
        return result, asked

    def test_the_question_reaches_the_user_and_the_answer_the_model(self, make_ctx):
        result, asked = self.ask(
            make_ctx,
            Answer(["SQLite"]),
            question="Which database?",
            options=[{"label": "SQLite", "description": "embedded"}, "PostgreSQL"],
        )
        assert asked == [("Which database?", [Choice("SQLite", "embedded"), Choice("PostgreSQL")], False, True)]
        assert result.success and result.data == {"answered": True, "selected": ["SQLite"]}
        assert result.summary == "→ SQLite" and "Which database?" in result.detail

    def test_a_typed_answer_and_several_options(self, make_ctx):
        result, asked = self.ask(
            make_ctx, Answer(["a", "b"], "c too"), question="Which?", options=["a", "b"], multiple=True, allow_other=False
        )
        assert asked[0][2:] == (True, False)
        assert result.data == {"answered": True, "selected": ["a", "b"], "other_answer": "c too"}
        assert result.summary == "→ a, b, “c too”"

    def test_a_skipped_question(self, make_ctx):
        result, _ = self.ask(make_ctx, None, question="Which?", options=["a"])
        assert result.success and result.data["answered"] is False and result.summary == "skipped"

    @pytest.mark.parametrize(
        "arguments",
        [
            {"question": "  ", "options": ["a"]},
            {"question": "Q?", "options": [], "allow_other": False},
            {"question": "Q?", "options": [{"description": "no label"}]},
            {"question": "Q?", "options": ["a", "a"]},
            {"question": "Q?", "options": [str(i) for i in range(12)]},
            {"question": "Q?", "options": [3]},
        ],
    )
    def test_bad_calls_fail_without_asking(self, make_ctx, arguments):
        result, asked = self.ask(make_ctx, Answer(["a"]), **arguments)
        assert not result.success and asked == []

    def test_the_tool_exists_only_with_someone_to_ask(self, ctx):
        assert ask_tools(ctx) == [] and "ask_user" not in [t.__name__ for t in build_tools(ctx)]

    def test_plain_string_options(self):
        assert parse_options(["x", " y "]) == [Choice("x"), Choice("y")]


# --------------------------------------------------------------------------- #
# Read-only tools, as sub-agents see them
# --------------------------------------------------------------------------- #


class TestMaxLevel:
    def test_guarded_tools_know_the_most_they_can_need(self, ctx):
        @guarded(ctx, PermissionLevel.READ)
        def fixed():
            pass

        @guarded(ctx, zone_level(ctx, PermissionLevel.WRITE, "path"))
        def by_zone(path: str):
            pass

        @guarded(ctx, lambda **kw: PermissionLevel.NONE)
        def unknown():
            pass

        assert fixed.max_level == PermissionLevel.READ
        assert by_zone.max_level == PermissionLevel.WRITE
        assert unknown.max_level == PermissionLevel.WRITE  # a rule of its own: assume the worst

    def test_sub_agents_get_read_only_tools_unless_writes_are_allowed(self, ctx):
        tools = build_tools(ctx)
        read_only = {t.__name__ for t in subagent_tools(tools, False)}
        everything = {t.__name__ for t in subagent_tools(tools, True)}
        assert {"file_system_read", "file_system_grep", "file_system_glob"} <= read_only
        assert not read_only & {"file_system_write", "file_system_edit", "file_system_remove", "run_command"}
        assert {"file_system_write", "run_command"} <= everything
        assert not everything & EXCLUDED_TOOLS


# --------------------------------------------------------------------------- #
# task
# --------------------------------------------------------------------------- #


class FakeSubAgent:
    """Calls the tool hook like agno would, then reports."""

    def __init__(self, tools, hook, instructions, script):
        self.tools = {tool.__name__: tool for tool in tools}
        self.hook, self.instructions, self.script = hook, instructions, script
        self.outputs = []

    def run(self, prompt, **kwargs):
        from agno.metrics import RunMetrics
        from agno.run.agent import RunOutput

        self.prompt, self.kwargs = prompt, kwargs
        for name, arguments in self.script.get("calls", []):
            tool = self.tools.get(name, lambda **kw: ToolResult.fail(LookupError(f"no tool {name}")))
            self.outputs.append(json.loads(self.hook(name, tool, arguments)))
        if self.script.get("cancel"):
            self.script["cancel"].set()
        yield SimpleNamespace(event="ModelRequestCompleted", output_tokens=40)
        for chunk in self.script.get("answer", ["Found it ", "in a.py."]):
            yield SimpleNamespace(event="RunContent", content=chunk)
        if self.script.get("error"):
            raise self.script["error"]
        yield RunOutput(content="", metrics=RunMetrics(input_tokens=100, output_tokens=50, total_tokens=150))


@pytest.fixture
def harness(make_ctx, tmp_path):
    """A session in the middle of a turn, the `task` tool, and what the sub-agent did."""

    def factory(**script):
        ctx, gate = make_ctx(auto_level=1)
        (tmp_path / "files" / "a.py").write_text("x = 1\n")
        session = AgentSession(
            JsonlLogger(tmp_path / "log.jsonl"), "u", ContextManager(base_session_id="s"), UsageLedger(None), model="m"
        )
        cancel = threading.Event()
        script.setdefault("cancel_event", cancel)
        if script.pop("cancel_midway", False):
            script["cancel"] = cancel
        view = TurnView("hi")
        session._view, session._cancel = view, cancel
        built = {}

        def build(tools, hook, instructions):
            built["agent"] = FakeSubAgent(tools, hook, instructions, script)
            return built["agent"]

        subagents = SubAgents(session, lambda: build_tools(ctx), build, location=lambda: "/work")
        ctx.run_subagent = subagents.run
        task = tool_named(task_tools(ctx), "task")

        def call(**arguments):
            return json.loads(session.tool_hook("task", task, arguments))

        return SimpleNamespace(call=call, view=view, built=built, session=session, gate=gate)

    return factory


class TestTask:
    def test_the_report_comes_back_and_the_work_is_listed_under_the_task_line(self, harness):
        h = harness(calls=[("file_system_read", {"path": "a.py"}), ("file_system_grep", {"pattern": "x"})])
        output = h.call(description="find x", prompt="Where is x defined?")

        assert output == {"success": True, "data": {"report": "Found it in a.py."}}
        agent = h.built["agent"]
        assert agent.prompt == "Where is x defined?" and agent.instructions.endswith("Working directory: /work")
        assert agent.kwargs["stream"] and agent.kwargs["stream_events"]
        assert [o["success"] for o in agent.outputs] == [True, True]
        line = h.view.snapshot()[0]
        assert line.text.startswith("✔ task(") and "· 2 tool call(s)" in line.text
        assert line.detail.splitlines()[0].startswith("✔ file_system_read(path='a.py')")
        assert line.detail.endswith("Found it in a.py.")
        assert h.view.output_tokens == 42  # the sub-agent's tokens count in the turn: 40 reported + 2 chunks since

    def test_its_tokens_are_recorded_in_the_usage_ledger(self, harness):
        h = harness()
        h.call(description="d", prompt="p")
        assert h.session.usage.session_models["m"].total_tokens == 150

    def test_read_only_by_default(self, harness):
        h = harness(calls=[("file_system_write", {"path": "b.py", "content": "y"})])
        h.call(description="d", prompt="p")
        assert "no tool file_system_write" in h.built["agent"].outputs[0]["error"]

    def test_writes_when_allowed_still_go_through_the_gate(self, harness, tmp_path):
        h = harness(calls=[("file_system_write", {"path": str(tmp_path / "elsewhere.txt"), "content": "y"})])
        h.call(description="d", prompt="p", allow_writes=True)
        assert h.built["agent"].outputs[0]["success"] and len(h.gate.asked) == 1  # outside the zone: asked

    def test_it_never_gets_the_conversation_tools(self, harness):
        h = harness()
        h.call(description="d", prompt="p", allow_writes=True)
        assert not set(h.built["agent"].tools) & EXCLUDED_TOOLS

    def test_a_crash_is_a_failed_result_with_what_was_said(self, harness):
        h = harness(answer=["partial"], error=RuntimeError("model died"))
        output = h.call(description="d", prompt="p")
        assert output["success"] is False and "model died" in output["error"]
        assert output["data"] == {"partial_report": "partial"}

    def test_an_interruption_stops_it(self, harness):
        h = harness(cancel_midway=True)
        output = h.call(description="d", prompt="p")
        assert output["success"] is False and "Interrupted" in output["error"]

    def test_an_empty_answer_is_a_failure(self, harness):
        assert harness(answer=[]).call(description="d", prompt="p")["success"] is False

    def test_an_empty_prompt_is_refused(self, harness):
        h = harness()
        assert h.call(description="d", prompt=" ")["success"] is False and "agent" not in h.built


# --------------------------------------------------------------------------- #
# The transcript viewer's content
# --------------------------------------------------------------------------- #


def transcript_turn():
    view = TurnView("edit a.py")
    view.add_text("I will **edit** it.")
    note = view.tool_started("file_system_edit", {"path": "a.py"})
    view.tool_finished(note, "file_system_edit", {"path": "a.py"}, True, 0.1, summary="+1 −1", detail="-a\n+b", detail_kind="diff")
    plain = view.tool_started("file_system_pwd", {})
    view.tool_finished(plain, "file_system_pwd", {}, True, 0.0, summary="/work", detail="")
    view.show_todos("☑ step")
    view.stats = TurnStats(1, 2, 3, 1.0)
    return view


def text_of(rows):
    return ["".join(text for _, text in row) for row in rows]


class TestTranscriptItems:
    def test_tool_lines_with_something_to_hide_are_foldable(self):
        items = build_items([transcript_turn()], 80)
        foldable = [item for item in items if item.foldable]
        assert len(foldable) == 1
        assert text_of(foldable[0].rows)[0].startswith("✔ file_system_edit")
        assert [text.strip() for text in text_of(foldable[0].hidden)] == ["-a", "+b"]
        assert foldable[0].shown() == foldable[0].rows
        foldable[0].open = True
        assert len(foldable[0].shown()) == 3

    def test_everything_of_the_turn_is_there_in_order(self):
        rows = [text for item in build_items([transcript_turn()], 80) for text in text_of(item.shown())]
        joined = "\n".join(rows)
        assert rows[0].startswith("── 1 ") and rows[1] == "❯ edit a.py"
        assert "I will edit it." in joined and "**" not in joined  # Markdown, rendered
        assert joined.index("edit it") < joined.index("file_system_edit") < joined.index("☑ step") < joined.index("tokens")

    def test_a_diff_of_an_older_session_folds_under_its_tool_line(self):
        old = TurnView.from_dict(
            {"prompt": "p", "segments": [{"kind": "note", "text": "✔ edit", "style": "tool"}, {"kind": "diff", "text": "-x\n+y"}]}
        )
        foldable = [item for item in build_items([old], 80) if item.foldable]
        assert len(foldable) == 1 and text_of(foldable[0].rows) == ["✔ edit"]
