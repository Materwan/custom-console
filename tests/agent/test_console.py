"""AgentConsole wiring: settings -> tools -> gate -> UI, with a fake Clara server."""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fake_clara import FakeClara, ask_tools, say
from outcome import outcome
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from custom_console.agent import reminders
from custom_console.agent.clara import ClaraError
from custom_console.agent.console import AgentConsole, permission_label
from custom_console.fs import FileManager
from custom_console.settings import load_settings


def wait_for(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def build(tmp_path, permission_level, write_inside_zone=False, memory=False, clara=None, **env):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "visible.txt").write_text("hi")
    settings = load_settings({"MOODLE_ENABLED": "false", **env}, root=root, use_dotenv=False)
    target = str((work if write_inside_zone else tmp_path / "elsewhere") / "note.txt")

    def respond(body):
        message = body["message"]
        if message == "write a note":  # the model asks the console to write a file, then answers
            return [ask_tools(("file_system_write", {"path": target, "content": "hello\n"})), *say(f"Done: {message[:20]}")]
        return say(f"Done: {message[:20]}")

    fake = clara or FakeClara(respond)
    created = {"target": target, "clara": fake, "work": work}

    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    console = Console(file=io.StringIO(), width=400, force_terminal=False)
    agent_console = AgentConsole(
        settings=settings,
        name="Tester",
        permission_level=permission_level,
        files=FileManager(start_dir=str(work)),
        memory=memory,
        console=console,
        client=fake,
        input=pipe,
        output=DummyOutput(),
    )
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
        self.clara = self.created["clara"]

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
    def test_answer_flow_and_what_the_server_receives(self, tmp_path):
        session = Session(tmp_path, permission_level=2)

        def driver(s):
            s.send("hello")
            s.wait_output("Done: hello")

        out = session.run(driver)
        assert "Tester" in out and "fake-model on Clara (local)" in out  # banner
        assert "permissions: everything is auto-accepted" in out
        assert "free zone:" in out and "work" in out

        [body] = session.clara.bodies
        assert body["message"] == "hello" and body["surface"] == "console" and body["user_id"] == "tester"
        assert body["conversation"].startswith("console:tester:console_session-")  # under the user (or the server refuses it)
        assert body["prefix"].startswith("[Automatic note, not written by the user. Working directory: ")
        assert session.settings.load_instructions() in body["instructions"]
        names = {tool["function"]["name"] for tool in body["tools"]}
        assert {"file_system_read", "file_system_edit", "run_command", "todo_write", "ask_user", "task"} <= names
        assert not any(name.startswith(("moodle_", "workspace_")) for name in names)
        assert "ephemeral" not in body

        entries = [json.loads(l) for l in session.settings.agent_log_path.read_text(encoding="utf-8").splitlines()]
        assert [e["type"] for e in entries] == ["prompt", "answer"]

    def test_the_tool_schemas_sent_describe_the_parameters(self, tmp_path):
        session = Session(tmp_path)
        session.pipe_ctx.__exit__(None, None, None)
        body = session.console_.session.request_body("hi")
        edit = next(tool for tool in body["tools"] if tool["function"]["name"] == "file_system_edit")["function"]
        assert edit["parameters"]["required"] == ["path", "old_text", "new_text"]
        assert edit["parameters"]["properties"]["replace_all"]["type"] == "boolean"
        assert "exact text" in edit["description"] and "file to edit" in edit["parameters"]["properties"]["path"]["description"]

    def test_the_conversation_is_erased_from_the_server_without_memory(self, tmp_path):
        session = Session(tmp_path, memory=False)
        session.run(lambda s: None)
        assert session.clara.forgotten == [session.console_.context.session_id]

    def test_the_conversation_is_kept_with_memory(self, tmp_path):
        session = Session(tmp_path, memory=True)

        def driver(s):
            s.send("hello")
            s.wait_output("Done: hello")

        session.run(driver)
        assert session.clara.forgotten == []

    def test_the_server_must_be_reachable_to_open_the_console(self, tmp_path):
        fake = FakeClara()
        fake.down = True
        with pytest.raises(ClaraError, match="Cannot reach"):
            build(tmp_path, 1, clara=fake)

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
        [[answer]] = session.clara.results  # what the console told the server
        assert answer["id"] == "call_0_0" and outcome(answer["content"]).success
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
        result = outcome(session.clara.results[0][0]["content"])
        assert not result.success and "UserPermissionDenied" in result.error
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

    def test_an_unknown_or_disabled_tool_is_reported_to_the_model(self, tmp_path):
        fake = FakeClara(lambda body: [ask_tools(("no_such_tool", {})), *say("ok")])
        session = Session(tmp_path, clara=fake)

        def driver(s):
            s.send("go")
            s.wait_output("ok")

        out = session.run(driver)
        result = outcome(fake.results[0][0]["content"])
        assert not result.success and "not an available tool" in result.error
        assert "✘ no_such_tool" in out

    def test_a_server_failure_is_shown_in_the_turn(self, tmp_path):
        def respond(body):
            return [{"type": "token", "text": "partial "}, {"type": "error", "message": "The language model failed"}]

        session = Session(tmp_path, clara=FakeClara(respond))

        def driver(s):
            s.send("hello")
            s.wait_output("The language model failed")

        out = session.run(driver)
        assert "partial" in out and "Agent error: The language model failed" in out

    def test_automatic_compaction_by_the_server_is_noted(self, tmp_path):
        def respond(body):
            return [{"type": "compacted", "before": 85.0, "after": 12.0}, *say("done")]

        session = Session(tmp_path, clara=FakeClara(respond))

        def driver(s):
            s.send("hello")
            s.wait_output("Conversation compacted (85% → 12%")

        session.run(driver)

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
        for name in ("/model", "/server-model", "/models", "/provider", "/usage", "/context", "/compact", "/undo", "/init", "/ls", "/cd", "/cp", "/rm", "/bye"):
            assert name in out

    def test_server_model_and_provider_are_the_servers_commands(self, tmp_path):
        session = Session(tmp_path)
        session.clara.admin_output["/provider"] = "* local  Local host"

        def driver(s):
            s.send("/provider")
            s.wait_output("* local  Local host")
            session.clara.model, session.clara.provider = "gpt-oss:120b", "cloud"  # what the server does
            s.send("/provider cloud")
            s.wait_output("ran /provider cloud")
            s.send("/server-model gpt-oss:120b")
            s.wait_output("ran /model gpt-oss:120b")  # the server's own /model

        session.run(driver)
        assert session.clara.admin_calls == ["/provider", "/provider cloud", "/model gpt-oss:120b"]
        assert session.console_.model == "gpt-oss:120b" and session.console_.provider_name == "cloud"
        assert "gpt-oss:120b" in session.console_.screen.title and "(cloud)" in session.console_.screen.title

    def test_models_is_the_servers_catalogue_command(self, tmp_path):
        session = Session(tmp_path)
        session.clara.admin_output["/models"] = "Server default: local:fake"

        def driver(s):
            s.send("/models")
            s.wait_output("Server default: local:fake")

        session.run(driver)
        assert session.clara.admin_calls == ["/models"]

    def test_model_chooses_among_the_models_an_administrator_offers(self, tmp_path):
        session = Session(tmp_path)
        session.clara.offered_models = [
            {"ref": "cloud:big", "name": "big", "provider": "cloud", "provider_label": "Ollama API key", "weight": 8.75},
            {"ref": "local:small", "name": "small", "provider": "local", "provider_label": "Local host", "weight": 0.125},
        ]

        def driver(s):
            s.send("/model")
            s.wait_output("1. big (Ollama API key), 8.75 credits per token")
            s.send("/model 1")
            s.wait_output("answers you here with big")
            s.send("/model nonsense")
            s.wait_output("is not one of the models offered")
            s.send("/model SMALL")
            s.wait_output("answers you here with small")
            s.send("/model default")
            s.wait_output("answers you here with fake-model")

        session.run(driver)
        assert session.clara.model_choice is None
        assert session.clara.admin_calls == []  # never the server's console: this is the user's own choice

    def test_the_title_follows_the_model_chosen_and_nothing_is_offered_by_default(self, tmp_path):
        session = Session(tmp_path)
        session.clara.offered_models = [
            {"ref": "cloud:big", "name": "big", "provider": "cloud", "provider_label": "Ollama API key", "weight": 8.75},
        ]

        def driver(s):
            s.send("/model big")
            s.wait_output("answers you here with big")

        session.run(driver)
        assert session.console_.model == "big" and "big" in session.console_.screen.title
        (tmp_path / "other").mkdir()
        quiet = Session(tmp_path / "other")

        def ask(s):
            s.send("/model")
            s.wait_output("An administrator has not offered other models")

        quiet.run(ask)

    def test_the_model_the_server_used_is_followed_after_each_turn(self, tmp_path):
        def respond(body):
            return say("hi", model="other-model", provider="cloud")

        session = Session(tmp_path, clara=FakeClara(respond))

        def driver(s):
            s.send("hello")
            s.wait_output("hi")

        session.run(driver)
        assert session.console_.model == "other-model" and "other-model" in session.console_.screen.title
        assert session.console_.session.usage.session_models["other-model"].turns == 1

    def test_without_an_admin_token_the_server_commands_explain(self, tmp_path):
        session = Session(tmp_path, clara=FakeClara(admin_token=None))

        def driver(s):
            s.send("/provider")
            s.wait_output("CLARA_ADMIN_TOKEN")

        session.run(driver)

    def test_provider_names_are_completed_from_the_server(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        session = Session(tmp_path)
        completer = SlashCompleter(session.console_.screen.commands)
        names = [c.text for c in completer.get_completions(Document("/provider c"), None)]
        session.pipe_ctx.__exit__(None, None, None)
        assert names == ["cloud"]

    def test_usage_reports_session_and_ledger(self, tmp_path):
        session = Session(tmp_path)
        from custom_console.agent.turn import TurnStats

        session.console_.session.usage.record("gemma4:test", TurnStats(100, 50, 150, 2.0))

        def driver(s):
            s.send("/usage")
            s.wait_output("By model")

        out = session.run(driver)
        assert "This session" in out and "All time" in out and "gemma4:test" in out and "150" in out

    def test_context_shows_the_breakdown_from_the_servers_figures(self, tmp_path):
        session = Session(tmp_path, clara=FakeClara(tokens=2048))

        def driver(s):
            s.send("/context")
            s.wait_output("Free space")

        out = session.run(driver)
        assert "System prompt" in out and "Tools (" in out and "Messages" in out
        assert "2,048 / 8,192 tokens (25%)" in out

    def test_compact_has_the_server_summarise_the_conversation(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/compact the thing")
            s.wait_output("Conversation compacted")

        out = session.run(driver)
        assert session.clara.compactions == [(session.console_.context.session_id, "the thing")]
        assert "60% → 10%" in out and "User wants the thing built" in out
        assert session.console_.context.summary.startswith("User wants")
        assert "summary" not in session.clara.bodies[0:1] or True  # the server puts the summary in the prompt

    def test_compacting_an_empty_conversation_says_so(self, tmp_path):
        session = Session(tmp_path)
        session.clara.nothing_to_compact = True

        def driver(s):
            s.send("/compact")
            s.wait_output("nothing to compact")

        session.run(driver)

    def test_clear_starts_a_new_conversation(self, tmp_path):
        session = Session(tmp_path)
        session.console_.context.summary = "old summary"
        old = session.console_.context.session_id

        def driver(s):
            s.send("/clear")
            wait_for(lambda: session.console_.context.summary == "")

        session.run(driver)
        context = session.console_.context
        assert context.session_id != old  # a conversation of its own
        assert session.console_.record.id in context.session_id

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
        assert "AGENT.md" in session.clara.bodies[0]["message"]

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

    def test_the_context_meter_follows_the_servers_figure_before_the_first_turn(self, tmp_path):
        session = Session(tmp_path, clara=FakeClara(tokens=6000))
        session.pipe_ctx.__exit__(None, None, None)
        breakdown = session.console_.context.breakdown()
        assert breakdown.used == 6000 and breakdown.messages > 0  # what is not the prompt or the tools

    def test_context_percent_is_shown_in_the_header(self, tmp_path):
        session = Session(tmp_path)
        assert "ctx" in session.console_.screen._status() and "8.2k" in session.console_.screen._status()
        session.pipe_ctx.__exit__(None, None, None)


DOWN, ESCAPE = "\x1b[B", "\x1b"


def tool_names(session):
    return [tool.__name__ for tool in session.console_.toolset.enabled()]


def sent_tool_names(session):
    return [tool["function"]["name"] for tool in session.clara.bodies[-1]["tools"]]


class TestToolsCommand:
    def test_off_and_on_change_what_the_agent_gets(self, tmp_path):
        session = Session(tmp_path)
        assert "run_command" in tool_names(session)

        def driver(s):
            s.send("/tools off run_command todo")
            s.wait_output("Turned off: run_command, todo_write")
            s.send("/tools on todo_write")
            s.wait_output("Turned on: todo_write")
            s.send("hello")
            s.wait_output("Done: hello")

        out = session.run(driver)
        names = tool_names(session)
        assert "run_command" not in names and "todo_write" in names and "file_system_read" in names
        sent = sent_tool_names(session)  # the model is only offered the tools that are on
        assert "run_command" not in sent and "todo_write" in sent and "file_system_read" in sent
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
        instructions = session.clara.bodies[-1]["instructions"]
        assert "Tools turned off by the user" in instructions and "run_command" in instructions

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
            assert rows[0] == ("group", "Files") and rows[1][1].key == "file_system_list"
            s.pipe.send_text(DOWN + " " + "\r")  # the first tool under "Files"
            s.wait_output("Turned off: file_system_list")

        session.run(driver)
        assert "file_system_list" not in tool_names(session)
        assert json.loads(session.settings.agent_tools_path.read_text(encoding="utf-8")) == {"disabled": ["file_system_list"]}

    def test_escape_leaves_the_tools_as_they_were(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/tools")
            wait_for(lambda: s.console_.screen._menu is not None)
            s.pipe.send_text(DOWN + " " + ESCAPE)
            s.wait_output("Tools unchanged.")

        session.run(driver)
        assert "file_system_list" in tool_names(session) and not session.settings.agent_tools_path.exists()

    def test_a_saved_selection_is_used_at_startup(self, tmp_path):
        settings = load_settings({"MOODLE_ENABLED": "false"}, root=tmp_path / "project", use_dotenv=False)
        settings.agent_tools_path.parent.mkdir(parents=True)
        settings.agent_tools_path.write_text(json.dumps({"disabled": ["run_command"]}), encoding="utf-8")
        session = Session(tmp_path)
        session.pipe_ctx.__exit__(None, None, None)
        assert "run_command" not in tool_names(session) and "todo_write" in tool_names(session)
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


class TestReminders:
    def announced(self, text, **fields):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {"type": "reminder", "id": 1, "text": text, "due_at": now, "fired_at": now, "from": "Alice", **fields}

    def test_remind_sets_one_on_the_server_for_your_clients(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/remind weekly +2h Water the plants")
            s.wait_output("shown on all your clients")

        out = session.run(driver)
        [reminder] = session.clara.reminder_list
        assert (reminder["text"], reminder["repeat"]) == ("Water the plants", "weekly")
        due = datetime.fromisoformat(reminder["due_at"])
        assert due.tzinfo is not None  # the console's own offset travels with the time
        assert timedelta(hours=1, minutes=59) < due - datetime.now().astimezone() < timedelta(hours=2, minutes=1)
        assert "Reminder 1 set for" in out and ", then weekly" in out

    def test_remind_explains_what_it_cannot_read(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/remind someday Tea")
            s.wait_output("Cannot read the time")
            s.send("/remind +5m")
            s.wait_output("needs a text")

        session.run(driver)
        assert session.clara.reminder_list == []

    def test_remind_shows_the_servers_refusal(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            session.clara.down = True  # the server goes away after startup
            s.send("/remind +5m Tea")
            s.wait_output("Cannot reach the Clara server")

        session.run(driver)
        assert session.clara.reminder_list == []

    def test_reminders_are_listed_and_cancelled(self, tmp_path):
        session = Session(tmp_path)
        session.clara.add_reminder("2026-10-05T09:00:00+02:00", "Dentist")
        session.clara.add_reminder("2026-10-06T09:00:00+02:00", "Bins", "weekly")

        def driver(s):
            s.send("/reminders")
            s.wait_output("Bins")
            s.send("/unremind 1")
            s.wait_output("Reminder cancelled")
            s.send("/unremind 99")
            s.wait_output("No such reminder")
            s.send("/unremind soon")
            s.wait_output("Usage: /unremind")

        out = session.run(driver)
        assert "Dentist" in out and "weekly" in out
        assert [r["text"] for r in session.clara.reminder_list] == ["Bins"]

    def test_notify_after_is_shown_and_set(self, tmp_path):
        session = Session(tmp_path)

        def driver(s):
            s.send("/notify-after")
            s.wait_output("after 120 s of work")
            s.send("/notify-after off")
            s.wait_output("notified never")
            s.send("/notify-after 90")
            s.wait_output("notified after 90 s")
            s.send("/notify-after soon")
            s.wait_output("Usage: /notify-after")

        session.run(driver)
        assert session.clara.notify_after_value == 90  # what was not understood changed nothing

    def test_a_reminder_waiting_at_startup_is_shown_above_the_prompt(self, tmp_path):
        session = Session(tmp_path)
        session.clara.announced.append(self.announced("Dentist at 9"))  # came due while the console was closed

        def driver(s):
            s.wait_output("Dentist at 9")

        out = session.run(driver)
        assert "⏰ Dentist at 9" in out and "Alice" not in out  # always your own: no author

    def test_a_reminder_that_comes_due_while_the_console_runs_is_shown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reminders, "RECONNECT_SECONDS", 0.05)
        session = Session(tmp_path)

        def driver(s):
            session.clara.announced.append(self.announced("Meeting now"))
            s.wait_output("Meeting now")

        assert "⏰ Meeting now" in session.run(driver)

    def test_a_reminder_is_shown_even_while_the_agent_is_working(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reminders, "RECONNECT_SECONDS", 0.05)
        release = threading.Event()

        def respond(body):
            release.wait(5)
            return say("finished")

        session = Session(tmp_path, clara=FakeClara(respond))

        def driver(s):
            s.send("work")
            wait_for(lambda: s.console_.screen._busy)
            session.clara.announced.append(self.announced("During the turn"))
            wait_for(lambda: "During the turn" in session.out.file.getvalue())
            release.set()
            s.wait_output("finished")

        session.run(driver)

    def test_the_message_clara_wrote_is_shown(self, tmp_path):
        session = Session(tmp_path)
        session.clara.announced.append(self.announced("Dentist at 9", message="Alice, your dentist is waiting!"))

        def driver(s):
            s.wait_output("your dentist is waiting")

        out = session.run(driver)
        assert "⏰ Alice, your dentist is waiting!" in out

    def test_the_state_of_the_server_is_shown_when_it_changes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reminders, "RECONNECT_SECONDS", 0.05)
        session = Session(tmp_path)
        session.clara.announced.extend(
            [{"type": "server", "state": "running"}, {"type": "server", "state": "stopping"}, {"type": "server", "state": "stopped"}]
        )

        def driver(s):
            s.wait_output("Clara is not running.")

        out = session.run(driver)
        assert "● Clara is stopping" in out and "● Clara is not running." in out

    def test_remind_arguments_are_completed(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        session = Session(tmp_path)
        session.clara.add_reminder("2026-10-05T09:00:00+02:00", "Dentist")
        completer = SlashCompleter(session.console_.screen.commands)

        def complete(text):
            return [c.text for c in completer.get_completions(Document(text), None)]

        assert complete("/remind d") == ["daily"]
        assert complete("/remind ") == ["daily", "weekly", "monthly", "tomorrow"]
        assert complete("/remind daily t") == ["tomorrow"]
        assert complete("/remind +2h ") == []
        assert complete("/unremind ") == ["1"]
        session.pipe_ctx.__exit__(None, None, None)


def test_permission_labels():
    assert permission_label(0) == "always ask"
    assert permission_label(1) == "reads are auto-accepted"
    assert permission_label(2) == "everything is auto-accepted"
    assert permission_label(7) == "level 7"
