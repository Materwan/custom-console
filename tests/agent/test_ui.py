"""Behavioural tests of AgentScreen, driven with real keystrokes through a pipe."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from typing import Callable

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from custom_console.agent.turn import TurnStats, TurnView
from custom_console.agent.ui import AgentScreen, MenuItem

CTRL_C = "\x03"
CTRL_D = "\x04"
CTRL_U = "\x15"


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

    def __init__(self, runner, banner=None, **screen_options):
        self.screen_options = screen_options
        self.console = Console(file=io.StringIO(), width=80, force_terminal=False)
        self.runner = runner
        self.banner = banner
        self.screen = None
        self.pipe = None
        self.errors = []

    @property
    def output(self) -> str:
        return self.console.file.getvalue()

    def idle(self) -> bool:
        return not self.screen._busy and self.screen._question is None

    def send(self, text: str) -> None:
        self.pipe.send_text(text)

    def run(self, driver: Callable[["Harness"], None], timeout: float = 15.0) -> str:
        with create_pipe_input() as pipe:
            self.pipe = pipe
            self.screen = AgentScreen(
                title="Test Agent",
                console=self.console,
                turn_runner=self.runner,
                banner=self.banner,
                input=pipe,
                output=DummyOutput(),
                **self.screen_options,
            )

            def drive():
                try:
                    wait_for(lambda: self.screen._loop is not None)
                    time.sleep(0.1)
                    driver(self)
                except BaseException as error:  # surface driver failures in the test
                    self.errors.append(error)
                finally:
                    try:  # a busy screen keeps typed text instead of acting on it
                        wait_for(self.idle, timeout=5)
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

    def test_plain_text_streams_in_the_live_area_while_the_agent_works(self):
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
            seen["live"] = joined(h.screen._live_fragments())
            seen["out_before_end"] = h.output
            release.set()
            wait_for(h.idle)

        out = h.run(driver)
        assert "streaming **now**" in seen["live"]  # raw plain text
        assert "streaming" not in seen["out_before_end"]  # an unfinished paragraph stays live
        assert "streaming" in out and "**now**" not in out  # replaced by the Markdown rendering

    def test_finished_paragraphs_reach_the_scrollback_while_the_agent_works(self):
        release, started = threading.Event(), threading.Event()

        def runner(view, cancel):
            view.add_text("First **paragraph**.\n\nSecond one, still")
            started.set()
            release.wait(5)
            view.add_text(" being written.")

        h = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            assert started.wait(5)
            wait_for(lambda: "First paragraph." in h.output)  # printed, as Markdown, before the end
            seen["live"] = joined(h.screen._live_fragments())
            release.set()
            wait_for(h.idle)

        out = h.run(driver)
        assert "First" not in seen["live"] and "Second one, still" in seen["live"]
        assert out.count("First paragraph.") == 1 and "Second one, still being written." in out
        assert out.index("❯ go") < out.index("First paragraph.") < out.index("Second one")

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

    def test_a_message_sent_while_busy_is_queued_then_sent(self):
        release = threading.Event()
        calls = []

        def runner(view, cancel):
            calls.append(view.prompt)
            if view.prompt == "one":
                release.wait(5)

        h = Harness(runner)

        def driver(h):
            h.send("one\r")
            wait_for(lambda: calls == ["one"])
            h.send("two\r")  # typed while busy
            h.send("three\r")
            wait_for(lambda: len(h.screen._queue) == 2)
            assert calls == ["one"] and h.screen._buffer.text == ""
            assert "(2 waiting)" in joined(h.screen._header_fragments())
            release.set()
            wait_for(lambda: calls == ["one", "two", "three"] and h.idle())

        h.run(driver)
        assert calls == ["one", "two", "three"]

    def test_ctrl_c_drops_the_queue(self):
        calls = []

        def runner(view, cancel):
            calls.append(view.prompt)
            cancel.wait(5)

        h = Harness(runner)

        def driver(h):
            h.send("one\r")
            wait_for(lambda: calls == ["one"])
            h.send("two\r")
            wait_for(lambda: h.screen._queue == ["two"])
            h.send(CTRL_C)
            wait_for(h.idle)
            time.sleep(0.2)

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
                console=Console(file=io.StringIO()),
                turn_runner=simple_runner,
                input=pipe,
                output=DummyOutput(),
            )
            with pytest.raises(RuntimeError, match="not running"):
                screen.ask_permission("x")


class TestSlashCommands:
    def test_help_and_unknown_command(self):
        h = Harness(simple_runner)

        def driver(h):
            h.send("/help\r")
            wait_for(lambda: "/bye" in h.output)
            h.send("/nonsense\r")
            wait_for(lambda: "Unknown command: /nonsense" in h.output)

        out = h.run(driver)
        assert "/clear" in out and "Ctrl+C" in out

    def test_commands_are_not_sent_to_the_agent(self):
        calls = []
        h = Harness(lambda v, c: calls.append(v.prompt))

        def driver(h):
            h.send("/help\r")
            wait_for(lambda: "/bye" in h.output)

        h.run(driver)
        assert calls == []

    def test_clear_redraws_the_banner(self):
        h = Harness(simple_runner, banner="BANNER-TEXT")

        def driver(h):
            assert h.output.count("BANNER-TEXT") == 1  # printed at startup
            h.send("/clear\r")
            wait_for(lambda: h.output.count("BANNER-TEXT") == 2)

        h.run(driver)


class TestLayout:
    def test_live_area_is_bounded_by_the_terminal_height(self):
        release = threading.Event()

        def runner(view, cancel):
            view.add_text("\n".join(f"line {i}" for i in range(500)))
            release.wait(5)

        h = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._view is not None and h.screen._view.snapshot())
            text = joined(h.screen._live_fragments())
            seen["rows"] = text.count("\n") + 1
            seen["last"] = text.splitlines()[-1]
            release.set()
            wait_for(h.idle)

        h.run(driver)
        assert seen["rows"] <= 40  # DummyOutput is 40 rows tall
        assert seen["last"] == "line 499"

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


DOWN, UP, ENTER, ESCAPE = "\x1b[B", "\x1b[A", "\r", "\x1b"
PAGE_DOWN, HOME, END = "\x1b[6~", "\x1b[H", "\x1b[F"


def small_menu():
    return [
        MenuItem("a", "alpha", "first", group="One"),
        MenuItem("b", "beta", "second", group="One"),
        MenuItem("c", "gamma", "third", group="Two", checked=False),
    ]  # rows: 0 One, 1 alpha, 2 beta, 3 Two, 4 gamma


def menu_harness(items):
    """A harness whose agent turn opens a menu; `answers` collects what it returns."""
    answers, cancels, holder = [], [], {}

    def runner(view, cancel):
        answers.append(holder["h"].screen.ask_menu("Pick tools", items))
        cancels.append(cancel.is_set())
        view.add_text("done")

    holder["h"] = Harness(runner)
    return holder["h"], answers, cancels


def open_menu(h):
    h.send("go\r")
    wait_for(lambda: h.screen._menu is not None)


class TestMenu:
    def test_items_toggle_and_the_answer_maps_each_key_to_its_state(self):
        h, answers, _ = menu_harness(small_menu())

        def driver(h):
            open_menu(h)
            h.send(DOWN + " ")  # alpha off
            h.send(DOWN * 3 + " ")  # gamma on
            wait_for(lambda: h.screen._menu.cursor == 4)
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [{"a": False, "b": True, "c": True}]

    def test_space_on_a_group_turns_all_its_items_on_or_off(self):
        h, answers, _ = menu_harness(small_menu())

        def driver(h):
            open_menu(h)
            h.send(" ")  # on the first group: all on -> all off
            wait_for(lambda: not h.screen._menu.items[0].checked)
            h.send(DOWN * 3 + " ")  # the second group: all off -> all on
            wait_for(lambda: h.screen._menu.items[2].checked)
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [{"a": False, "b": False, "c": True}]

    def test_a_turns_everything_on_then_off(self):
        h, answers, _ = menu_harness(small_menu())

        def driver(h):
            open_menu(h)
            h.send("a")
            wait_for(lambda: all(item.checked for item in h.screen._menu.items))
            h.send("a")
            wait_for(lambda: not any(item.checked for item in h.screen._menu.items))
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [{"a": False, "b": False, "c": False}]

    @pytest.mark.parametrize("key", [ESCAPE, CTRL_C])
    def test_escape_and_ctrl_c_cancel_without_stopping_the_turn(self, key):
        h, answers, cancels = menu_harness(small_menu())

        def driver(h):
            open_menu(h)
            h.send(DOWN + " ")  # a change that must not survive
            h.send(key)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [None] and cancels == [False]

    def test_typing_in_a_menu_does_not_reach_the_input_line(self):
        h, answers, _ = menu_harness(small_menu())

        def driver(h):
            open_menu(h)
            h.send("xyz")
            h.send(DOWN + UP)  # must not recall the history either
            time.sleep(0.3)
            assert h.screen._buffer.text == ""
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert h.screen._buffer.text == "" and answers == [{"a": True, "b": True, "c": False}]

    def test_the_menu_is_drawn_with_checkboxes_groups_and_key_hints(self):
        h, answers, _ = menu_harness(small_menu())
        seen = {}

        def driver(h):
            open_menu(h)
            seen["lines"] = [text for _, text in h.screen._menu_lines(80)]
            seen["header"] = joined(h.screen._header_fragments())
            seen["prompt"] = joined(h.screen._prompt_fragments())
            h.send(DOWN + " ")
            wait_for(lambda: not h.screen._menu.items[0].checked)
            seen["partial"] = h.screen._menu_lines(80)[1][1]
            h.send(ESCAPE)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        lines = seen["lines"]
        assert lines[0] == "? Pick tools"
        assert "[x] One  (2/2)" in lines[1] and lines[1].startswith("❯")  # the cursor starts on the first row
        assert "[x] alpha" in lines[2] and "first" in lines[2]
        assert "[ ] gamma" in lines[5] and "[ ] Two  (0/1)" in lines[4]
        assert lines[-1].strip() == "2/3 on"
        assert "Space toggle" in seen["header"] and "menu" in seen["prompt"]
        assert "[-] One  (1/2)" in seen["partial"]

    def test_a_long_menu_scrolls_to_keep_the_cursor_visible(self):
        items = [MenuItem(f"t{i}", f"tool_{i:02d}", group="G") for i in range(80)]
        h, answers, _ = menu_harness(items)
        seen = {}

        def driver(h):
            open_menu(h)
            seen["first"] = [text for _, text in h.screen._menu_lines(80)]
            h.send(END)
            wait_for(lambda: h.screen._menu.cursor == 80)
            seen["last"] = [text for _, text in h.screen._menu_lines(80)]
            h.send(HOME + PAGE_DOWN)
            wait_for(lambda: h.screen._menu.cursor == 35)  # a page: the 35 rows of list that a 40-row terminal leaves
            seen["page"] = [text for _, text in h.screen._menu_lines(80)]
            h.send(ESCAPE)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert len(seen["first"]) <= 40 - 2 and "↓" in seen["first"][-1] and "↑" not in seen["first"][-1]
        assert seen["last"][-2].lstrip("❯ ").strip().endswith("tool_79") or "tool_79" in seen["last"][-2]
        assert "↑" in seen["last"][-1] and "↓" not in seen["last"][-1]
        assert any(text.startswith("❯") and "tool_34" in text for text in seen["page"])

    def test_asking_outside_a_running_screen_fails_clearly(self):
        with create_pipe_input() as pipe:
            screen = AgentScreen(
                title="t", console=Console(file=io.StringIO()), turn_runner=simple_runner, input=pipe, output=DummyOutput()
            )
            with pytest.raises(RuntimeError, match="not running"):
                screen.ask_menu("x", small_menu())
