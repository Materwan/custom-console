"""AgentConsole wiring: settings -> tools -> gate -> UI, with a fake agno agent."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from custom_console.agent.console import AgentConsole, permission_label
from custom_console.fs import FileManager
from custom_console.settings import load_settings


def wait_for(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


class ScriptedAgent:
    """Fake agno agent: calls the tool hook like agno would, then answers."""

    def __init__(self, tools, hook, calls):
        self.tools = {tool.__name__: tool for tool in tools}
        self.hook = hook
        self.calls = calls
        self.tool_output = None

    def run(self, prompt, **kwargs):
        if prompt == "write a note":
            self.tool_output = self.hook(
                "workspace_file_write",
                self.tools["workspace_file_write"],
                {"directory": "tmp", "path": "note.txt", "content": "hello"},
            )
        yield SimpleNamespace(event="RunContent", content=f"Done: {prompt}")


def build(tmp_path, permission_level):
    root = tmp_path / "project"
    root.mkdir()
    settings = load_settings({"MOODLE_ENABLED": "false"}, root=root, use_dotenv=False)
    created = {}

    def factory(settings_, model, name, tools, hook, memory):
        created.update(model=model, name=name, memory=memory, tool_names=[t.__name__ for t in tools])
        agent = ScriptedAgent(tools, hook, created)
        created["agent"] = agent
        return agent

    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    agent_console = AgentConsole(
        settings=settings,
        model="gemma4:test",
        name="Tester",
        permission_level=permission_level,
        files=FileManager(start_dir=str(tmp_path)),
        memory=False,
        agent_factory=factory,
        input=pipe,
        output=DummyOutput(),
    )
    return agent_console, settings, created, agent_console.screen.transcript_text, pipe, pipe_ctx


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
            try:
                wait_for(lambda: not agent_console.screen._busy, timeout=3)  # /bye is only taken between turns
            except AssertionError:
                pass
            pipe.send_text("\x15/bye\r")

    threading.Thread(target=drive, daemon=True).start()
    agent_console.run()
    if errors:
        raise errors[0]


class TestAgentConsole:
    def test_answer_flow_and_factory_arguments(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=2)
        try:
            def driver():
                pipe.send_text("hello\r")
                wait_for(lambda: "Done: hello" in console() and not agent_console.screen._busy)

            run_with(agent_console, pipe, driver)
        finally:
            pipe_ctx.__exit__(None, None, None)

        out = console()
        assert "Tester" in out and "gemma4:test" in out  # banner
        assert "permissions: everything is auto-accepted" in out
        assert created["model"] == "gemma4:test" and created["memory"] is False
        assert "file_system_read" in created["tool_names"] and "workspace_file_write" in created["tool_names"]
        assert not any(name.startswith("moodle_") for name in created["tool_names"])  # MOODLE_ENABLED=false

        entries = [json.loads(l) for l in settings.agent_log_path.read_text(encoding="utf-8").splitlines()]
        assert [e["type"] for e in entries] == ["prompt", "answer", "turn"]
        assert entries[0]["prompt"] == "hello"

    def test_write_tool_asks_in_the_ui_at_level_1_and_runs_when_accepted(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=1)
        try:
            def driver():
                pipe.send_text("write a note\r")
                wait_for(lambda: agent_console.screen._question is not None)
                assert "workspace file write" in agent_console.screen._question.info
                pipe.send_text("y\r")
                wait_for(lambda: "Done: write a note" in console() and not agent_console.screen._busy)

            run_with(agent_console, pipe, driver)
        finally:
            pipe_ctx.__exit__(None, None, None)

        assert (settings.workspace_roots["tmp"] / "note.txt").read_text() == "hello"
        assert created["agent"].tool_output == "tmp/note.txt"
        out = console()
        assert "→ accepted" in out and "✔ workspace_file_write" in out

    def test_refusal_prevents_the_tool_from_running(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=1)
        try:
            def driver():
                pipe.send_text("write a note\r")
                wait_for(lambda: agent_console.screen._question is not None)
                pipe.send_text("n\r")
                wait_for(lambda: "Done: write a note" in console() and not agent_console.screen._busy)

            run_with(agent_console, pipe, driver)
        finally:
            pipe_ctx.__exit__(None, None, None)

        assert not (settings.workspace_roots["tmp"] / "note.txt").exists()
        assert created["agent"].tool_output.startswith("ERROR UserPermissionDenied")
        assert "→ refused" in console()

    def test_level_2_does_not_ask(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=2)
        asked = []
        try:
            def driver():
                pipe.send_text("write a note\r")
                wait_for(lambda: "Done: write a note" in console() and not agent_console.screen._busy)
                asked.append(agent_console.screen._question)

            run_with(agent_console, pipe, driver)
        finally:
            pipe_ctx.__exit__(None, None, None)

        assert asked == [None]
        assert (settings.workspace_roots["tmp"] / "note.txt").read_text() == "hello"
        assert "→ auto-accepted" in console()

    def test_tool_context_is_closed_when_the_console_exits(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=1)
        closed = []
        agent_console.tool_context.on_close(lambda: closed.append(True))
        try:
            run_with(agent_console, pipe, lambda: None)
        finally:
            pipe_ctx.__exit__(None, None, None)
        assert closed == [True]

    def test_history_file_lives_in_the_data_directory(self, tmp_path):
        agent_console, settings, created, console, pipe, pipe_ctx = build(tmp_path, permission_level=1)
        try:
            def driver():
                pipe.send_text("remember me\r")
                wait_for(lambda: "Done: remember me" in console() and not agent_console.screen._busy)

            run_with(agent_console, pipe, driver)
        finally:
            pipe_ctx.__exit__(None, None, None)
        assert "remember me" in settings.agent_history_path.read_text(encoding="utf-8")


def test_permission_labels():
    assert permission_label(0) == "always ask"
    assert permission_label(1) == "reads are auto-accepted"
    assert permission_label(2) == "everything is auto-accepted"
    assert permission_label(7) == "level 7"
