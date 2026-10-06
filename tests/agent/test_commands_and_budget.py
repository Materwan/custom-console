"""Slash commands of the agent console, and the token-saving behaviour of the tools."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from custom_console.agent.commands import AgentCommands, pick_model
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.permissions import PermissionGate
from custom_console.agent.session import AgentSession
from custom_console.agent.tools import build_tools
from custom_console.agent.tools.base import ToolMemo, cap_items
from custom_console.agent.turn import TurnStats, TurnView
from custom_console.llm.ollama import ModelInfo, OllamaUnavailableError

MODELS = [
    ModelInfo("gemma4:latest", 5 * 1024**3, ["completion", "tools"]),
    ModelInfo("llama3:8b", 4 * 1024**3, ["completion", "tools", "thinking"]),
    ModelInfo("embed:1b", 1024**3, ["embedding"]),
    ModelInfo("big-cloud", None, ["completion", "tools"]),
]


class FakeOllama:
    def __init__(self, models=MODELS, running=("gemma4:latest",)):
        self.models, self._running, self.started = models, set(running), []

    def installed(self):
        return list(self.models)

    def running(self):
        return [m for m in self.models if m.name in self._running]

    def is_running(self, name):
        return name in self._running

    def start(self, name):
        self.started.append(name)
        self._running.add(name)


class FakeConsole:
    def __init__(self, ollama=None):
        self.settings = SimpleNamespace(default_model="gemma4")
        self.model = "gemma4:latest"
        self.ollama = ollama or FakeOllama()
        self.switched, self.new_calls = [], 0
        self.tool_context = SimpleNamespace(gate=PermissionGate(1, lambda i: True))
        self.session = SimpleNamespace(turns=0, totals=TurnStats(), last=None)

    def switch_model(self, name):
        self.switched.append(name)
        self.model = name

    def new_conversation(self):
        self.new_calls += 1


def text_of(renderable) -> str:
    if isinstance(renderable, str):
        return renderable
    console = Console(width=100, record=True, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def console():
    return FakeConsole()


@pytest.fixture
def commands(console):
    return AgentCommands(console)


class TestPickModel:
    def test_by_number_name_or_bare_name(self):
        assert pick_model(MODELS, "2").name == "llama3:8b"
        assert pick_model(MODELS, "llama3:8b").name == "llama3:8b"
        assert pick_model(MODELS, "llama3").name == "llama3:8b"
        assert pick_model(MODELS, "llama3:70b") is None
        assert pick_model(MODELS, "0") is None and pick_model(MODELS, "9") is None


class TestModelCommand:
    def test_lists_models_with_the_active_one_marked(self, commands):
        out = text_of(commands.model(""))
        assert "gemma4:latest" in out and "llama3:8b" in out
        assert "●" in out and "loaded" in out and "cloud" in out
        assert "no tools" in out  # the embedding model is listed as unusable
        assert commands.all()[0].choices() == [m.name for m in MODELS] + ["default"]

    def test_switches_by_number_and_loads_a_model_that_is_not_running(self, commands, console):
        out = text_of(commands.model("2"))
        assert console.switched == ["llama3:8b"] and "llama3:8b" in out
        assert console.ollama.started == ["llama3:8b"]

    def test_a_cloud_model_is_not_loaded(self, commands, console):
        commands.model("big-cloud")
        assert console.switched == ["big-cloud"] and console.ollama.started == []

    def test_default_returns_to_the_configured_model(self, commands, console):
        console.model = "llama3:8b"
        commands.model("default")
        assert console.switched == ["gemma4:latest"]

    @pytest.mark.parametrize(
        "args, message",
        [("nope", "No installed model"), ("embed", "cannot call tools"), ("gemma4", "Already answering")],
    )
    def test_refusals_do_not_switch(self, commands, console, args, message):
        assert message in text_of(commands.model(args))
        assert console.switched == []

    def test_an_unreachable_ollama_is_reported_by_the_screen(self, console):
        class Down(FakeOllama):
            def installed(self):
                raise OllamaUnavailableError("cannot reach Ollama")

        commands = AgentCommands(FakeConsole(Down()))
        with pytest.raises(RuntimeError, match="cannot reach Ollama"):
            commands.model("")


class TestOtherCommands:
    def test_new_starts_a_conversation(self, commands, console):
        commands.new("")
        assert console.new_calls == 1

    def test_permissions_show_and_set(self, commands, console):
        assert text_of(commands.permissions("")).splitlines()[1].startswith("●")  # level 1 is active
        commands.permissions("2")
        assert console.tool_context.gate.auto_level == 2
        assert "Use /permissions" in text_of(commands.permissions("7"))
        assert console.tool_context.gate.auto_level == 2

    def test_tokens(self, commands, console):
        assert commands.tokens("") == "No answer yet." or "No answer yet" in text_of(commands.tokens(""))
        console.session = SimpleNamespace(
            turns=2, last=TurnStats(10, 5, 15), totals=TurnStats(30, 10, 40, 2.0)
        )
        out = text_of(commands.tokens(""))
        assert "10 in + 5 out" in out and "2 answers" in out and "20 per answer" in out


class TestSessionUsage:
    def make(self, tmp_path):
        return AgentSession(JsonlLogger(tmp_path / "log.jsonl"), "u", "s")

    def test_new_conversation_changes_the_session_and_resets_the_counters(self, tmp_path):
        session = self.make(tmp_path)
        session.turns, session.totals = 3, TurnStats(1, 1, 2)
        session.new_conversation()
        assert session.session_id.startswith("s-") and session.turns == 0 and session.totals == TurnStats()

    def test_turn_usage_is_accumulated_and_journaled(self, tmp_path):
        from agno.run.agent import RunOutput
        from agno.metrics import RunMetrics

        session = self.make(tmp_path)

        class Agent:
            def run(self, prompt, **kwargs):
                session.tool_hook("t", lambda **kw: "x" * 40, {})
                yield RunOutput(content="", metrics=RunMetrics(input_tokens=7, output_tokens=3, total_tokens=10))

        session.agent = Agent()
        session.run_turn(TurnView("hi"), threading.Event())
        session.run_turn(TurnView("again"), threading.Event())
        assert session.turns == 2 and session.totals.total_tokens == 20 and session.last.input_tokens == 7
        import json

        turn = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines() if '"turn"' in l][0]
        assert turn["total_tokens"] == 10 and turn["tool_calls"] == 1 and turn["tool_result_chars"] == 40


class TestToolBudget:
    def test_cap_items(self):
        assert cap_items([1, 2], 5) == [1, 2]
        assert cap_items(list("abcde"), 2) == ["a", "b", "[+3 more; narrow the path or pattern]"]

    def test_long_listings_are_capped(self, make_ctx, files_dir):
        ctx, _ = make_ctx()
        for i in range(200):
            (files_dir / f"f{i:03}.txt").write_text("x")
        tools = {t.__name__: t for t in build_tools(ctx)}
        data = tools["file_system_list"](str(files_dir)).data
        assert len(data) == 151 and data[-1].startswith("[+50 more")

    def test_identical_reads_are_answered_from_memory_until_something_is_written(self, make_ctx, files_dir):
        ctx, gate = make_ctx(auto_level=2)
        tools = {t.__name__: t for t in build_tools(ctx)}
        (files_dir / "a.txt").write_text("one")
        first = tools["file_system_read"](str(files_dir / "a.txt"))
        (files_dir / "a.txt").write_text("two")  # changed behind the agent's back
        assert tools["file_system_read"](str(files_dir / "a.txt")) is first  # remembered
        assert len(gate.recorded) == 1  # and not even asked again

        tools["workspace_file_write"]("tmp", "n.txt", "hi")  # a write clears the memory
        assert tools["file_system_read"](str(files_dir / "a.txt")).data == "two"

    def test_cd_clears_the_memory_so_relative_paths_stay_right(self, make_ctx, files_dir):
        ctx, _ = make_ctx()
        (files_dir / "sub").mkdir()
        (files_dir / "sub" / "only-here.txt").write_text("x")
        tools = {t.__name__: t for t in build_tools(ctx)}
        tools["file_system_cd"](str(files_dir))
        assert "only-here.txt" not in str(tools["file_system_list"](".").data)
        tools["file_system_cd"]("sub")
        assert "only-here.txt" in tools["file_system_list"](".").data

    def test_failures_are_not_remembered(self, make_ctx, files_dir):
        ctx, _ = make_ctx()
        tools = {t.__name__: t for t in build_tools(ctx)}
        path = str(files_dir / "late.txt")
        assert not tools["file_system_read"](path).success
        (files_dir / "late.txt").write_text("now")
        assert tools["file_system_read"](path).data == "now"

    def test_memo_entries_expire(self):
        now = [0.0]
        memo = ToolMemo(ttl=30, clock=lambda: now[0])
        memo.put("k", 1)
        assert memo.get("k") == 1
        now[0] = 31
        assert memo.get("k") is None
