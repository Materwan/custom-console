"""Behavioural tests of AgentScreen, driven with real keystrokes through a pipe."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.table import Table

from custom_console.agent.turn import TurnStats, TurnView
from custom_console.agent.ui import AgentScreen, SlashCommand, SlashCompleter

CTRL_C = "\x03"
CTRL_D = "\x04"
CTRL_U = "\x15"
PAGE_UP = "\x1b[5~"
PAGE_DOWN = "\x1b[6~"


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def joined(fragments) -> str:
    return "".join(text for _, text in fragments)


class Harness:
    """Runs an AgentScreen and lets a driver thread type into it."""

    def __init__(self, runner, banner=None, commands=()):
        self.runner = runner
        self.banner = banner
        self.commands = commands
        self.screen = None
        self.pipe = None
        self.errors = []

    @property
    def output(self) -> str:
        """The conversation as the user sees it (plain text)."""
        return self.screen.transcript_text() if self.screen else ""

    def idle(self) -> bool:
        return not self.screen._busy and self.screen._question is None

    def send(self, text: str) -> None:
        self.pipe.send_text(text)

    def run(self, driver: Callable[["Harness"], None], timeout: float = 15.0) -> str:
        with create_pipe_input() as pipe:
            self.pipe = pipe
            self.screen = AgentScreen(
                title="Test Agent",
                turn_runner=self.runner,
                banner=self.banner,
                commands=self.commands,
                input=pipe,
                output=DummyOutput(),
            )

            def drive():
                try:
                    wait_for(lambda: self.screen._loop is not None)
                    time.sleep(0.1)
                    driver(self)
                except BaseException as error:  # surface driver failures in the test
                    self.errors.append(error)
                finally:
                    try:
                        wait_for(self.idle, timeout=3)  # /bye is only taken between turns
                    except AssertionError:
                        pass
                    pipe.send_text(CTRL_U + "/bye\r")

            thread = threading.Thread(target=drive, daemon=True)
            thread.start()
            asyncio.run(asyncio.wait_for(self.screen.run_async(), timeout))
            thread.join(timeout=5)
        if self.errors:
            raise self.errors[0]
        return self.output


def simple_runner(view: TurnView, cancel: threading.Event):
    view.add_text("# Answer\n\nThis is **bold**.")
    return TurnStats(1, 2, 3, 1.0)


class TestTurns:
    def test_answer_is_printed_as_markdown_after_the_turn(self):
        h = Harness(simple_runner)

        def driver(h):
            h.send("hello there\r")
            wait_for(lambda: "tokens" in h.output and h.idle())

        out = h.run(driver)
        assert "❯ hello there" in out
        assert "Answer" in out and "bold" in out
        assert "**bold**" not in out and "# Answer" not in out  # rendered, not raw
        assert "1 prompt + 2 completion = 3 tokens" in out

    def test_the_turn_is_visible_while_the_agent_works(self):
        release = threading.Event()
        started = threading.Event()

        def runner(view, cancel):
            view.add_text("streaming **now**")
            started.set()
            release.wait(5)

        h = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            assert started.wait(5)
            seen["live"] = h.output
            release.set()
            wait_for(h.idle)

        out = h.run(driver)
        assert "❯ go" in seen["live"] and "streaming now" in seen["live"]
        assert "streaming now" in out and "**now**" not in out  # rendered as Markdown

    def test_live_area_disappears_when_the_turn_ends(self):
        h = Harness(simple_runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: "tokens" in h.output and h.idle())
            seen["live_after"] = h.screen._view

        h.run(driver)
        assert seen["live_after"] is None

    def test_empty_input_is_ignored(self):
        calls = []
        h = Harness(lambda v, c: calls.append(v.prompt))

        def driver(h):
            h.send("\r")
            h.send("   \r")
            time.sleep(0.3)

        h.run(driver)
        assert calls == []

    def test_two_turns_in_a_row(self):
        prompts = []

        def runner(view, cancel):
            prompts.append(view.prompt)
            view.add_text(f"reply to {view.prompt}")

        h = Harness(runner)

        def driver(h):
            for count, text in enumerate(("first", "second"), start=1):
                h.send(text + "\r")
                wait_for(lambda: len(prompts) == count and h.idle())
                time.sleep(0.1)

        out = h.run(driver)
        assert prompts == ["first", "second"]
        assert out.index("reply to first") < out.index("reply to second")

    def test_runner_exceptions_do_not_hang_the_ui(self):
        def runner(view, cancel):
            view.add_text("partial")
            raise RuntimeError("model exploded")

        h = Harness(runner)

        def driver(h):
            h.send("go\r")
            wait_for(lambda: "model exploded" in h.output and h.idle())
            h.send("again\r")  # the UI still accepts input afterwards
            wait_for(lambda: h.output.count("model exploded") == 2 and h.idle())

        out = h.run(driver)
        assert "Agent error: model exploded" in out and "partial" in out

    def test_enter_while_busy_does_not_start_a_second_turn(self):
        release = threading.Event()
        calls = []

        def runner(view, cancel):
            calls.append(view.prompt)
            release.wait(5)

        h = Harness(runner)

        def driver(h):
            h.send("one\r")
            wait_for(lambda: calls == ["one"])
            h.send("two\r")  # typed while busy
            time.sleep(0.3)
            assert calls == ["one"]
            assert h.screen._buffer.text == "two"  # kept in the input line
            release.set()
            wait_for(h.idle)

        h.run(driver)
        assert calls == ["one"]


class TestInterruption:
    def test_ctrl_c_cancels_the_running_turn_and_keeps_the_partial_answer(self):
        started = threading.Event()
        cancelled = threading.Event()

        def runner(view, cancel):
            view.add_text("partial answer")
            started.set()
            assert cancel.wait(5)
            cancelled.set()
            view.add_note("Interrupted by the user.")

        h = Harness(runner)

        def driver(h):
            h.send("go\r")
            assert started.wait(5)
            h.send(CTRL_C)
            assert cancelled.wait(5)
            wait_for(h.idle)

        out = h.run(driver)
        assert "partial answer" in out and "Interrupted by the user." in out

    def test_ctrl_c_when_idle_clears_the_input_line(self):
        h = Harness(simple_runner)

        def driver(h):
            h.send("some typed text")
            wait_for(lambda: h.screen._buffer.text == "some typed text")
            h.send(CTRL_C)
            wait_for(lambda: h.screen._buffer.text == "")

        h.run(driver)

    def test_ctrl_d_quits_when_idle_and_empty(self):
        h = Harness(simple_runner)
        exited = threading.Event()

        def driver(h):
            h.send(CTRL_D)
            wait_for(lambda: not h.screen._app.is_running)
            exited.set()

        h.run(driver)
        assert exited.is_set()

    def test_ctrl_d_is_ignored_when_text_is_typed(self):
        h = Harness(simple_runner)

        def driver(h):
            h.send("abc")
            wait_for(lambda: h.screen._buffer.text == "abc")
            h.send(CTRL_D)
            time.sleep(0.3)
            assert h.screen._app.is_running

        h.run(driver)


class TestPermissions:
    @pytest.mark.parametrize(
        "typed, expected",
        [("y\r", True), ("\r", True), ("yes\r", True), ("n\r", False), ("no\r", False), ("whatever\r", False)],
    )
    def test_answers_are_read_from_the_same_input_line(self, typed, expected):
        answers = []
        holder = {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_permission("Agent wants to copy."))
            view.add_text("done")

        h = holder["h"] = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._question is not None)
            seen["prompt"] = joined(h.screen._prompt_fragments())
            seen["question"] = joined(h.screen._question_fragments())
            seen["header"] = joined(h.screen._header_fragments())
            h.send(typed)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [expected]
        assert "Accept" in seen["prompt"] and "Agent wants to copy." in seen["question"]
        assert "waiting for your answer" in seen["header"]

    def test_ctrl_c_refuses_and_cancels(self):
        answers, cancels = [], []
        holder = {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_permission("Agent wants to delete."))
            cancels.append(cancel.is_set())

        h = holder["h"] = Harness(runner)

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._question is not None)
            h.send(CTRL_C)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [False] and cancels == [True]

    def test_asking_outside_a_running_screen_fails_clearly(self):
        with create_pipe_input() as pipe:
            screen = AgentScreen(
                title="t",
                turn_runner=simple_runner,
                input=pipe,
                output=DummyOutput(),
            )
            with pytest.raises(RuntimeError, match="not running"):
                screen.ask_permission("x")


class TestSlashCommands:
    def test_help_lists_builtin_and_given_commands(self):
        command = SlashCommand("model", "[name]", "pick a model", lambda args: None)
        h = Harness(simple_runner, commands=[command])

        def driver(h):
            h.send("/help\r")
            wait_for(lambda: "/bye" in h.output)
            h.send("/nonsense\r")
            wait_for(lambda: "Unknown command: /nonsense" in h.output)

        out = h.run(driver)
        assert "/clear" in out and "Ctrl+C" in out
        assert "/model [name]" in out and "pick a model" in out

    def test_commands_are_not_sent_to_the_agent(self):
        calls = []
        h = Harness(lambda v, c: calls.append(v.prompt))

        def driver(h):
            h.send("/help\r")
            wait_for(lambda: "/bye" in h.output)

        h.run(driver)
        assert calls == []

    def test_clear_empties_the_conversation_but_keeps_the_banner(self):
        h = Harness(simple_runner, banner="BANNER-TEXT")

        def driver(h):
            h.send("hello\r")
            wait_for(lambda: "tokens" in h.output and h.idle())
            h.send("/clear\r")
            wait_for(lambda: "hello" not in h.output)

        out = h.run(driver)
        assert "BANNER-TEXT" in out and "Answer" not in out

    def test_a_given_command_runs_with_its_arguments_and_shows_its_output(self):
        seen = []

        def handler(args):
            seen.append(args)
            table = Table()
            table.add_column("name")
            table.add_row("llama3")
            return table

        h = Harness(simple_runner, commands=[SlashCommand("model", "", "", handler)])

        def driver(h):
            h.send("/model  2\r")
            wait_for(lambda: "llama3" in h.output and h.idle())

        h.run(driver)
        assert seen == ["2"]

    def test_text_output_and_errors_of_a_command_are_shown(self):
        def failing(args):
            raise RuntimeError("ollama is down")

        h = Harness(
            simple_runner,
            commands=[SlashCommand("ok", "", "", lambda a: "all good"), SlashCommand("bad", "", "", failing)],
        )

        def driver(h):
            h.send("/ok\r")
            wait_for(lambda: "all good" in h.output and h.idle())
            h.send("/bad\r")
            wait_for(lambda: "/bad: ollama is down" in h.output and h.idle())
            h.send("again\r")  # the console is still usable
            wait_for(lambda: "Answer" in h.output)

        h.run(driver)

    def test_enter_during_a_command_keeps_the_text(self):
        release = threading.Event()

        def slow(args):
            release.wait(5)

        h = Harness(simple_runner, commands=[SlashCommand("slow", "", "", slow)])

        def driver(h):
            h.send("/slow\r")
            wait_for(lambda: h.screen._busy)
            h.send("hello\r")
            time.sleep(0.2)
            assert h.screen._buffer.text == "hello"
            assert "/slow" in joined(h.screen._header_fragments())
            release.set()
            wait_for(h.idle)

        h.run(driver)

    def test_completer_suggests_commands_and_first_arguments(self):
        command = SlashCommand("model", "", "pick", lambda a: None, lambda: ["gemma4", "llama3"])
        completer = SlashCompleter(lambda: {"model": command})

        def names(text):
            return [c.text for c in completer.get_completions(Document(text), None)]

        assert names("/mo") == ["/model"]
        assert names("/") == ["/model"]
        assert names("/model ll") == ["llama3"]
        assert names("/model ") == ["gemma4", "llama3"]
        assert names("hello") == []


class TestLayout:
    def test_screen_is_full_screen_with_the_input_as_the_last_row(self):
        h = Harness(simple_runner)

        def driver(h):
            assert h.screen._app.full_screen
            windows = h.screen._app.layout.container.content.children  # pane, question, header, entry
            assert windows[-1].content.buffer is h.screen._buffer

        h.run(driver)

    def test_conversation_follows_the_bottom_and_can_be_scrolled(self):
        def runner(view, cancel):
            view.add_text("\n\n".join(f"line {i}" for i in range(200)))

        h = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: "line 199" in h.output and h.idle())
            wait_for(lambda: h.screen._total > 60)  # the pane has rendered the long answer
            screen = h.screen
            seen["follow"], seen["bottom"] = screen._follow, screen._top
            h.send(PAGE_UP)
            wait_for(lambda: not screen._follow)
            seen["scrolled"] = screen._top
            seen["header"] = joined(screen._header_fragments())
            h.send(PAGE_DOWN * 50)
            wait_for(lambda: screen._follow)
            seen["back"] = screen._top

        h.run(driver)
        assert seen["follow"] and seen["bottom"] > 0
        assert 0 < seen["scrolled"] < seen["bottom"]
        assert "scrolled" in seen["header"] and len(seen["header"]) <= 80
        assert seen["back"] == seen["bottom"]

    def test_new_output_brings_the_view_back_to_the_bottom(self):
        h = Harness(lambda view, cancel: view.add_text("\n\n".join(str(i) for i in range(100))))

        def driver(h):
            h.send("go\r")
            wait_for(lambda: "99" in h.output and h.idle())
            wait_for(lambda: h.screen._total > 60)  # the pane has rendered the long answer
            h.send(PAGE_UP)
            wait_for(lambda: not h.screen._follow)
            h.send("again\r")
            wait_for(lambda: h.screen._follow)
            wait_for(h.idle)  # /bye is only taken between turns

        h.run(driver)

    def test_header_shows_the_activity_and_fits_the_width(self):
        release, started = threading.Event(), threading.Event()

        def runner(view, cancel):
            index = view.tool_started("file_system_list", {})
            started.set()
            release.wait(5)
            view.tool_finished(index, "file_system_list", {}, True, 0.1)

        h = Harness(runner)
        seen = {}

        def driver(h):
            seen["idle"] = joined(h.screen._header_fragments())
            h.send("go\r")
            assert started.wait(5)
            seen["busy"] = joined(h.screen._header_fragments())
            release.set()
            wait_for(h.idle)

        h.run(driver)
        assert "Test Agent" in seen["idle"] and "Enter: send" in seen["idle"]
        assert "running file_system_list" in seen["busy"]
        assert all(len(line) <= 80 for line in (seen["idle"], seen["busy"]))
