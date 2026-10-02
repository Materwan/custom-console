"""AgentConsole wiring: settings -> tools -> gate -> UI, with a fake agno agent and a fake Ollama."""

from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from custom_console.agent.console import AgentConsole, permission_label
from custom_console.agent.context import without_notes
from custom_console.fs import FileManager
from custom_console.llm.keys import KeyStore
from custom_console.llm.ollama import ModelInfo, OllamaUnavailableError
from custom_console.settings import load_settings


def wait_for(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


class FakeOllama:
    MODELS = [
        ModelInfo("gemma4:test", size=2_000_000_000, context_length=8192),
        ModelInfo("big:cloud", size=300, context_length=262144, remote=True),
        ModelInfo("other:1b", size=500_000_000, context_length=4096),
    ]

    def __init__(self, available=True):
        self.available = available

    def installed(self):
        if not self.available:
            raise OllamaUnavailableError("cannot reach Ollama")
        return list(self.MODELS)

    def info(self, name):
        models = self.installed()
        return next((m for m in models if m.name == name or m.name.split(":")[0] == name), None)


class ScriptedAgent:
    """Fake agno agent: calls the tool hook like agno would, then answers."""

    def __init__(self, tools, hook, calls, extra_args):
        self.tool_list = list(tools)
        self.hook = hook
        self.calls = calls
        self.extra_args = extra_args
        self.tool_output = None
        self.prompts = []
        self.inputs = []
        self.model = "initial"
        self.additional_context = None

    @property
    def tools(self):
        return {tool.__name__: tool for tool in self.tool_list}

    @tools.setter
    def tools(self, value):
        self.tool_list = list(value)

    def run(self, prompt, **kwargs):
        self.inputs.append(prompt)  # as the model gets it: after the <context> block
        prompt = without_notes(prompt)
        self.prompts.append(prompt)
        if prompt == "write a note":
            self.tool_output = self.hook(
                "file_system_write",
                self.tools["file_system_write"],
                {"path": self.extra_args["target"], "content": "hello\n"},
            )
        yield SimpleNamespace(event="RunContent", content=f"Done: {prompt[:20]}")

    def get_chat_history(self, session_id=None, last_n_runs=None):
        return [
            SimpleNamespace(role="user", content="Please build the thing", tool_calls=None),
            SimpleNamespace(role="assistant", content="Built it.", tool_calls=None),
        ]


class FakeKeyring:
    """Stands in for the Windows Credential Manager."""

    def __init__(self):
        self.saved = {}

    def get_password(self, service, name):
        return self.saved.get((service, name))

    def set_password(self, service, name, value):
        self.saved[(service, name)] = value

    def delete_password(self, service, name):
        del self.saved[(service, name)]


class FakeCatalog:
    """The models of a remote provider; `valid_key` is the only key it accepts."""

    def __init__(self, names, key, valid_key, context=128_000):
        self.names, self.key, self.valid_key, self.context = names, key, valid_key, context

    def installed(self):
        if self.key != self.valid_key:
            raise OllamaUnavailableError("refused the API key (HTTP 401)")
        return [ModelInfo(name, context_length=self.context, remote=True) for name in self.names]

    def info(self, name):
        return next((model for model in self.installed() if model.name == name), None)


REMOTE_MODELS = {"chatgpt": (["gpt-5-mini", "gpt-5", "gpt-4.1"], "sk-good"), "ollama-cloud": (["gpt-oss:120b", "glm-5.3"], "ol-good")}


def build(tmp_path, permission_level, write_inside_zone=False, memory=False, provider="ollama", model="gemma4:test", **env):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "visible.txt").write_text("hi")
    settings = load_settings({"MOODLE_ENABLED": "false", **env}, root=root, use_dotenv=False)
    created = {"target": str((work if write_inside_zone else tmp_path / "elsewhere") / "note.txt"), "agents": []}
    keyring = FakeKeyring()
    created["keyring"] = keyring

    def factory(settings_, model, name, tools, hook, memory, num_ctx, provider="ollama", api_key=None):
        created.update(
            model=model, name=name, memory=memory, num_ctx=num_ctx, tool_names=[t.__name__ for t in tools],
            provider=provider, api_key=api_key,
        )
        agent = ScriptedAgent(tools, hook, created, created)
        created["agent"] = agent
        return agent

    def model_factory(settings_, name, num_ctx, provider="ollama", api_key=None):
        created["switched_to"] = (name, num_ctx)
        created["switched_provider"] = (provider, api_key)
        return f"model:{name}"

    def catalogs(provider_, key):
        if provider_.local:
            return created["ollama"]
        names, valid = REMOTE_MODELS[provider_.name]
        return FakeCatalog(names, key, valid)

    created["ollama"] = FakeOllama()

    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    console = Console(file=io.StringIO(), width=400, force_terminal=False)
    agent_console = AgentConsole(
        settings=settings,
        model=model,
        name="Tester",
        permission_level=permission_level,
        files=FileManager(start_dir=str(work)),
        memory=memory,
        console=console,
        agent_factory=factory,
        model_factory=model_factory,
        ollama=created["ollama"],
        provider=provider,
        keys=KeyStore(env=env, backend=keyring),
        catalogs=catalogs,
        input=pipe,
        output=DummyOutput(),
    )
    created["work"] = work
    return agent_console, settings, created, console, pipe, pipe_ctx


def run_with(agent_console, pipe, driver):
    errors = []

    def drive():
        try:
            wait_for(lambda: agent_console.screen._loop is not None)
            time.sleep(0.1)
            driver()
        except BaseException as error:
            errors.append(error)
        finally:
            try:  # a busy screen keeps typed text instead of acting on it
                wait_for(lambda: not agent_console.screen._busy and agent_console.screen._question is None, timeout=5)
            except AssertionError:
                pass
            pipe.send_text("\x15/bye\r")

    threading.Thread(target=drive, daemon=True).start()
    agent_console.run()
    if errors:
        raise errors[0]


class Session:
    """Context manager running a console with a driver function."""

    def __init__(self, tmp_path, permission_level=1, **options):
        (self.console_, self.settings, self.created, self.out, self.pipe, self.pipe_ctx) = build(
            tmp_path, permission_level, **options
        )

    def run(self, driver):
        try:
            run_with(self.console_, self.pipe, lambda: driver(self))
        finally:
            self.pipe_ctx.__exit__(None, None, None)
        return self.out.file.getvalue()

    def send(self, text):
        self.pipe.send_text(text + "\r")

    def wait_output(self, text):
        """Wait for `text` to be printed, then for the screen to be ready for the next input
        (a busy screen keeps typed text instead of acting on it)."""
        wait_for(lambda: text in self.out.file.getvalue())
        wait_for(self.idle)

    def idle(self):
        screen = self.console_.screen
        return not screen._busy and screen._question is None


class TestAgentConsole:
    def test_answer_flow_and_factory_arguments(self, tmp_path):
        session = Session(tmp_path, permission_level=2)

        def driver(s):
            s.send("hello")
            s.wait_output("Done: hello")

        out = session.run(driver)
        created = session.created
        assert "Tester" in out and "gemma4:test" in out  # banner
        assert "permissions: everything is auto-accepted" in out
        assert "free zone:" in out and "work" in out
        assert created["model"] == "gemma4:test" and created["memory"] is False
        assert created["num_ctx"] == 8192  # local model: the window is requested from Ollama
        names = created["tool_names"]
        assert {"file_system_read", "file_system_edit", "run_command", "todo_write", "ask_user", "task"} <= set(names)
        assert not any(name.startswith(("moodle_", "workspace_")) for name in names)

        entries = [json.loads(l) for l in session.settings.agent_log_path.read_text(encoding="utf-8").splitlines()]
        assert [e["type"] for e in entries] == ["prompt", "answer"]

    def test_write_outside_the_zone_asks_at_level_1_and_runs_when_accepted(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("write a note")
            wait_for(lambda: s.console_.screen._question is not None)
            question = s.console_.screen._question.info
            assert "create" in question and "+hello" in question  # the content is shown
            s.send("y")
            s.wait_output("Done: write a note")

        out = session.run(driver)
        assert (tmp_path / "elsewhere" / "note.txt").read_text() == "hello\n"
        assert json.loads(session.created["agent"].tool_output)["success"] is True
        assert "→ accepted" in out and "✔ file_system_write" in out

    def test_refusal_prevents_the_tool_from_running(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("write a note")
            wait_for(lambda: s.console_.screen._question is not None)
            s.send("n")
            s.wait_output("Done: write a note")

        out = session.run(driver)
        assert not (tmp_path / "elsewhere" / "note.txt").exists()
        result = json.loads(session.created["agent"].tool_output)
        assert result["success"] is False and "UserPermissionDenied" in result["error"]
        assert "→ refused" in out

    def test_write_inside_the_zone_never_asks_even_at_level_0_and_folds_the_diff(self, tmp_path):
        session = Session(tmp_path, permission_level=0, write_inside_zone=True)
        asked = []

        def driver(s):
            s.send("write a note")
            s.wait_output("Done: write a note")
            asked.append(s.console_.screen._question)
            s.send("/details")  # from now on, tool lines are printed with what they hide
            s.wait_output("Tool details are shown")
            (s.created["work"] / "note.txt").unlink()  # created again: the whole file is the diff
            s.send("write a note")
            wait_for(lambda: s.out.file.getvalue().count("Done: write a note") == 2)

        out = session.run(driver)
        assert asked == [None]
        assert (session.created["work"] / "note.txt").read_text() == "hello\n"
        first, second = out.split("Tool details are shown")
        assert "✔ file_system_write" in first and "· +1 −0" in first and "+hello" not in first
        assert "+hello" in second  # the diff, unfolded
        saved = session.console_.record.turns[0]["segments"]
        assert any(segment.get("detail") == "+hello" for segment in saved)  # kept for /transcript

    def test_level_2_does_not_ask_outside_the_zone(self, tmp_path):
        session = Session(tmp_path, permission_level=2)

        def driver(s):
            s.send("write a note")
            s.wait_output("Done: write a note")

        out = session.run(driver)
        assert (tmp_path / "elsewhere" / "note.txt").read_text() == "hello\n"
        assert "→ auto-accepted" in out

    def test_tool_context_is_closed_when_the_console_exits(self, tmp_path):
        session = Session(tmp_path)
        closed = []
        session.console_.tool_context.on_close(lambda: closed.append(True))
        session.run(lambda s: None)
        assert closed == [True]

    def test_history_file_lives_in_the_data_directory(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("remember me")
            s.wait_output("Done: remember me")

        session.run(driver)
        assert "remember me" in session.settings.agent_history_path.read_text(encoding="utf-8")

    def test_no_zone_when_started_from_a_drive_root_or_home(self, tmp_path):
        from pathlib import Path

        from custom_console.agent.zone import FreeZone

        assert not FreeZone.around(Path.home()).active
        assert not FreeZone.around(Path.home().parent).active
        assert not FreeZone.around(Path(Path.home().anchor)).active
        assert FreeZone.around(tmp_path).active


class TestSlashCommands:
    def test_file_commands_work_on_the_agents_working_directory(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/ls")
            s.wait_output("visible.txt")
            s.send("/cat visible.txt")
            s.wait_output("hi")
            s.send("/cd ..")
            wait_for(s.idle)
            s.send("/pwd")
            wait_for(lambda: s.out.file.getvalue().count(str(tmp_path).replace("\\", "/")) >= 2)

        out = session.run(driver)
        assert session.console_.files.location == str(tmp_path).replace("\\", "/")  # shared with the agent's tools
        assert "\x1b" not in out

    def test_file_command_errors_are_shown_not_raised(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/cat nope.txt")
            s.wait_output("not found")
            s.send("/ls --bogus")
            s.wait_output("unrecognized")
            s.send("/ls -h")
            s.wait_output("usage: ls")

        session.run(driver)

    def test_rm_recursive_asks_in_the_input_line_and_defaults_to_no(self, tmp_path):
        session = Session(tmp_path)
        work = tmp_path / "work"
        (work / "folder").mkdir()

        def driver(s):
            s.send("/rm -r folder")
            wait_for(lambda: s.console_.screen._question is not None)
            assert s.console_.screen._question.default is False
            s.send("")  # empty answer = no
            wait_for(s.idle)
            assert (work / "folder").exists()
            s.send("/rm -r folder")
            wait_for(lambda: s.console_.screen._question is not None)
            s.send("y")
            wait_for(lambda: not (work / "folder").exists())

        session.run(driver)

    def test_help_lists_agent_and_file_commands(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/help")
            s.wait_output("Ctrl+C")

        out = session.run(driver)
        for name in ("/model", "/usage", "/context", "/compact", "/undo", "/init", "/ls", "/cd", "/cp", "/rm", "/bye"):
            assert name in out

    def test_model_lists_then_switches_and_keeps_the_conversation(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/model")
            s.wait_output("Installed models")
            s.send("/model other")
            s.wait_output("Model: other:1b")
            s.send("/model nothing-like-this")
            s.wait_output("is not installed")

        out = session.run(driver)
        assert "big:cloud" in out and "cloud" in out
        assert session.created["switched_to"] == ("other:1b", 4096)
        assert session.console_.model == "other:1b" and session.console_.context.window == 4096
        assert session.created["agent"].model == "model:other:1b"
        assert "other:1b" in session.console_.screen.title

    def test_switching_to_a_cloud_model_requests_no_context_size(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/model big:cloud")
            s.wait_output("Model: big:cloud")

        session.run(driver)
        assert session.created["switched_to"] == ("big:cloud", None)
        assert session.console_.context.window == 262144

    def test_usage_reports_session_and_ledger(self, tmp_path):
        session = Session(tmp_path)
        from custom_console.agent.turn import TurnStats

        session.console_.session.usage.record("gemma4:test", TurnStats(100, 50, 150, 2.0))

        def driver(s):
            s.send("/usage")
            s.wait_output("By model")

        out = session.run(driver)
        assert "This session" in out and "All time" in out and "gemma4:test" in out and "150" in out

    def test_context_shows_the_breakdown(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/context")
            s.wait_output("Free space")

        out = session.run(driver)
        assert "System prompt" in out and "Tools (" in out and "Messages" in out and "8,192" in out

    def test_compact_replaces_the_conversation_by_a_summary(self, tmp_path):
        session = Session(tmp_path)
        seen = {}

        def summarizer(transcript, previous, focus):
            seen.update(transcript=transcript, previous=previous, focus=focus)
            return "User wants the thing built; it is built."

        session.console_.session.summarize = summarizer

        def driver(s):
            s.send("/compact the thing")
            s.wait_output("Conversation compacted")

        out = session.run(driver)
        assert "Please build the thing" in seen["transcript"] and seen["focus"] == "the thing"
        assert "User wants the thing built" in out
        assert session.console_.context.summary.startswith("User wants")
        assert "summary" in session.created["agent"].additional_context.lower()
        assert session.console_.context.session_id == session.console_.context.base_session_id + "-1"
        assert session.console_.context.base_session_id.startswith("console_session-")

    def test_clear_starts_a_new_conversation(self, tmp_path):
        session = Session(tmp_path)
        session.console_.context.adopt_summary("old summary")
        old_base = session.console_.context.base_session_id

        def driver(s):
            s.send("/clear")
            wait_for(lambda: session.console_.context.summary == "")

        session.run(driver)
        context = session.console_.context
        assert context.generation == 0 and context.base_session_id != old_base  # a session of its own
        assert session.console_.record.id in context.base_session_id

    def test_undo_restores_what_the_last_turn_changed(self, tmp_path):
        session = Session(tmp_path, write_inside_zone=True)

        def driver(s):
            s.send("write a note")
            s.wait_output("Done: write a note")
            wait_for(s.idle)
            assert (s.created["work"] / "note.txt").exists()
            s.send("/undo")
            wait_for(lambda: not (s.created["work"] / "note.txt").exists())
            wait_for(s.idle)
            s.send("/undo")
            s.wait_output("Nothing to undo")

        session.run(driver)

    def test_init_hands_a_prompt_to_the_agent(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/init")
            s.wait_output("Done: Explore this project")

        session.run(driver)
        assert "AGENT.md" in session.created["agent"].prompts[0]

    def test_permissions_can_be_changed_while_running(self, tmp_path):
        session = Session(tmp_path, permission_level=1)

        def driver(s):
            s.send("/permissions")
            s.wait_output("Auto-accept level 1")
            s.send("/permissions 2")
            s.wait_output("Auto-accept level 2")
            s.send("/permissions 9")
            s.wait_output("Usage: /permissions")

        session.run(driver)
        assert session.console_.tool_context.gate.auto_level == 2

    def test_todo_command(self, tmp_path):
        session = Session(tmp_path)
        session.console_.tool_context.todos.replace([{"content": "step one", "status": "in_progress"}])

        def driver(s):
            s.send("/todo")
            s.wait_output("step one")

        assert "◐ step one" in session.run(driver)

    def test_file_command_arguments_complete_paths_and_flags(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        session = Session(tmp_path)
        completer = SlashCompleter(session.console_.screen.commands)
        paths = [c.text for c in completer.get_completions(Document("/cat vis"), None)]
        flags = [c.text for c in completer.get_completions(Document("/ls -"), None)]
        names = [c.display_text for c in completer.get_completions(Document("/c"), None)]
        session.pipe_ctx.__exit__(None, None, None)
        assert paths == ["visible.txt"] and "-a" in flags
        assert {"/cat", "/cd", "/clear", "/compact", "/context", "/cp"} == set(names)

    def test_the_context_meter_counts_a_conversation_resumed_from_memory(self, tmp_path):
        session = Session(tmp_path)
        session.pipe_ctx.__exit__(None, None, None)
        assert session.console_.context.breakdown().messages > 0  # the fake agent stores two messages

    def test_unavailable_ollama_is_reported(self, tmp_path):
        session = Session(tmp_path)
        session.created["ollama"].available = False

        def driver(s):
            s.send("/model")
            s.wait_output("cannot reach Ollama")

        session.run(driver)

    def test_context_percent_is_shown_in_the_header(self, tmp_path):
        session = Session(tmp_path)
        assert "ctx" in session.console_.screen._status() and "8.2k" in session.console_.screen._status()
        session.pipe_ctx.__exit__(None, None, None)


DOWN, ESCAPE = "\x1b[B", "\x1b"


def tool_names(session):
    return [tool.__name__ for tool in session.created["agent"].tool_list]


class TestToolsCommand:
    def test_off_and_on_change_what_the_agent_gets(self, tmp_path):
        session = Session(tmp_path)
        assert "run_command" in tool_names(session)

        def driver(s):
            s.send("/tools off run_command todo")
            s.wait_output("Turned off: run_command, todo_write")
            s.send("/tools on todo_write")
            s.wait_output("Turned on: todo_write")

        out = session.run(driver)
        names = tool_names(session)
        assert "run_command" not in names and "todo_write" in names and "file_system_read" in names
        assert session.console_.context.disabled_tools == ["run_command"]
        assert "tools are on (from the next message)" in out

    def test_the_agent_is_told_what_it_cannot_use(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/tools off run_command")
            s.wait_output("Turned off")
            s.send("hello")
            s.wait_output("Done: hello")

        session.run(driver)
        context = session.created["agent"].additional_context
        assert "Tools turned off by the user" in context and "run_command" in context

    def test_context_accounting_follows_the_selection(self, tmp_path):
        session = Session(tmp_path)
        before = session.console_.context.breakdown()

        def driver(s):
            s.send("/tools off files")
            s.wait_output("Turned off")

        session.run(driver)
        after = session.console_.context.breakdown()
        assert after.tool_count == before.tool_count - 14 and after.tools < before.tools

    def test_the_menu_applies_what_was_toggled_and_keeps_it(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/tools")
            wait_for(lambda: s.console_.screen._menu is not None)
            rows = s.console_.screen._menu.rows()
            assert rows[0] == ("group", "Files") and rows[1][1].key == "file_system_pwd"
            s.pipe.send_text(DOWN + " " + "\r")  # the first tool under "Files"
            s.wait_output("Turned off: file_system_pwd")

        session.run(driver)
        assert "file_system_pwd" not in tool_names(session)
        assert json.loads(session.settings.agent_tools_path.read_text(encoding="utf-8")) == {"disabled": ["file_system_pwd"]}

    def test_escape_leaves_the_tools_as_they_were(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/tools")
            wait_for(lambda: s.console_.screen._menu is not None)
            s.pipe.send_text(DOWN + " " + ESCAPE)
            s.wait_output("Tools unchanged.")

        session.run(driver)
        assert "file_system_pwd" in tool_names(session) and not session.settings.agent_tools_path.exists()

    def test_a_saved_selection_is_used_at_startup(self, tmp_path):
        settings = load_settings({"MOODLE_ENABLED": "false"}, root=tmp_path / "project", use_dotenv=False)
        settings.agent_tools_path.parent.mkdir(parents=True)
        settings.agent_tools_path.write_text(json.dumps({"disabled": ["run_command"]}), encoding="utf-8")
        session = Session(tmp_path)
        session.pipe_ctx.__exit__(None, None, None)
        assert "run_command" not in session.created["tool_names"] and "todo_write" in session.created["tool_names"]
        assert session.console_.context.disabled_tools == ["run_command"]

    def test_list_reset_and_mistakes(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/tools off run_command")
            s.wait_output("Turned off")
            s.send("/tools list")
            s.wait_output("What it does")
            s.send("/tools off nothing-like-it")
            s.wait_output("Unknown tool: nothing-like-it")
            s.send("/tools sideways")
            s.wait_output("Usage: /tools")
            s.send("/tools reset")
            s.wait_output("All ")

        out = session.run(driver)
        assert "OFF" in out and "run_command" in out
        assert session.console_.context.disabled_tools == [] and "run_command" in tool_names(session)

    def test_arguments_complete_subcommands_groups_and_the_right_tools(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        session = Session(tmp_path)
        session.console_.toolset.set_enabled({"file_system_read": False})
        session.console_.apply_tools()
        completer = SlashCompleter(session.console_.screen.commands)

        def complete(text):
            return [c.text for c in completer.get_completions(Document(text), None)]

        sub = complete("/tools o")
        offer_off = complete("/tools off file_system_r")
        offer_on = complete("/tools on file_system_r")
        groups = complete("/tools off fil")
        session.pipe_ctx.__exit__(None, None, None)
        assert sub == ["on", "off"]
        assert offer_off == ["file_system_remove"]  # `read` is already off
        assert offer_on == ["file_system_read"]  # `on` only suggests what is off
        assert "files" in groups

    def test_help_lists_tools_and_the_reMarkable_conversion(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/help")
            s.wait_output("Ctrl+C")

        out = session.run(driver)
        assert "/tools" in out and "/rmdoc2pdf" in out


class TestRmdocCommand:
    def test_slash_rmdoc_converts_a_document_in_the_working_directory(self, tmp_path, rm):
        session = Session(tmp_path)
        work = tmp_path / "work"
        rm.make_rmdoc(work / "Course.rmdoc", pdf=rm.make_pdf(2), redirects=[0, 1])

        def driver(s):
            s.send("/rmdoc Course.rmdoc")
            s.wait_output("2 page(s)")
            s.send("/rmdoc Course.rmdoc")
            s.wait_output("already exists")
            s.send("/rmdoc2pdf -f Course.rmdoc out.pdf")
            s.wait_output("out.pdf")

        session.run(driver)
        assert (work / "Course.pdf").read_bytes().startswith(b"%PDF") and (work / "out.pdf").exists()


def test_permission_labels():
    assert permission_label(0) == "always ask"
    assert permission_label(1) == "reads are auto-accepted"
    assert permission_label(2) == "everything is auto-accepted"
    assert permission_label(7) == "level 7"
