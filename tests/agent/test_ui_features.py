"""AgentScreen: the bottom bar, the multi-line input, the agent's questions, tool details and the transcript."""

from __future__ import annotations

import io
import re
import threading
import time

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from custom_console.agent.questions import Answer, Choice
from custom_console.agent.slash import CommandResult, Renderer, SlashCommand, SlashRegistry
from custom_console.agent.turn import TurnStats
from custom_console.agent.ui import AgentScreen, add_basic_commands, format_elapsed
from test_ui import CTRL_C, CTRL_U, DOWN, ENTER, ESCAPE, UP, Harness, joined, simple_runner, wait_for

SHIFT_TAB = "\x1b[Z"
SPACE = " "
CTRL_O, CTRL_T = "\x0f", "\x14"


def busy_runner(work=lambda view: None):
    """A runner that does `work`, then waits until released; returns (runner, started, release)."""
    started, release = threading.Event(), threading.Event()

    def runner(view, cancel):
        work(view)
        started.set()
        release.wait(5)

    return runner, started, release


def header_while_busy(h, started, release):
    seen = {}

    def driver(h):
        h.send("go\r")
        assert started.wait(5)
        seen["header"] = joined(h.screen._header_fragments())
        release.set()
        wait_for(h.idle)

    h.run(driver)
    return seen["header"]


# --------------------------------------------------------------------------- #
# The rule above the input
# --------------------------------------------------------------------------- #


class TestBottomBar:
    def test_time_tokens_and_checklist_progress_while_the_agent_works(self):
        def work(view):
            for chunk in ("a", "b", "c"):
                view.add_text(chunk)
            view.request_finished(1234)

        runner, started, release = busy_runner(work)
        header = header_while_busy(Harness(runner, todos=lambda: (2, 5, "Write the tests")), started, release)
        assert "thinking ·" in header and "s · ↓ 1.2k tokens" in header and "☑ 2/5 · Write the tests" in header
        assert len(header) <= 80

    def test_a_narrow_terminal_drops_the_checklist_text_before_the_clock(self):
        runner, started, release = busy_runner()
        long_step = "a fairly long description of the step that is in progress right now"
        header = header_while_busy(Harness(runner, todos=lambda: (1, 3, long_step)), started, release)
        assert "☑ 1/3" in header and "fairly long" not in header and "tokens" in header

    def test_elapsed_time_format(self):
        assert format_elapsed(8.42) == "8.4s" and format_elapsed(125) == "2m05s"

    def test_an_unfinished_checklist_is_recalled_when_idle(self):
        seen = {}
        h = Harness(simple_runner, todos=lambda: (1, 3, "next"))
        h.run(lambda h: seen.update(idle=joined(h.screen._header_fragments())))
        assert "☑ 1/3" in seen["idle"]

    def test_a_finished_checklist_is_not(self):
        seen = {}
        h = Harness(simple_runner, todos=lambda: (3, 3, ""))
        h.run(lambda h: seen.update(idle=joined(h.screen._header_fragments())))
        assert "☑" not in seen["idle"]


# --------------------------------------------------------------------------- #
# The input
# --------------------------------------------------------------------------- #


class TestInput:
    def test_shift_tab_starts_a_new_line_and_enter_sends_everything(self):
        prompts = []
        h = Harness(lambda view, cancel: prompts.append(view.prompt))
        seen = {}

        def driver(h):
            h.send("first line" + SHIFT_TAB + "second line")
            wait_for(lambda: h.screen._buffer.text == "first line\nsecond line")
            seen["rows"] = h.screen._input_rows()
            h.send(ENTER)
            wait_for(lambda: prompts and h.idle())

        h.run(driver)
        assert prompts == ["first line\nsecond line"] and seen["rows"] == 2

    def test_a_long_line_wraps_instead_of_being_cut(self):
        h = Harness(simple_runner)
        seen = {}

        def driver(h):
            h.send("x" * 200)
            wait_for(lambda: len(h.screen._buffer.text) == 200)
            seen["rows"] = h.screen._input_rows()
            seen["prefix"] = h.screen._line_prefix(0, 1)
            h.send(CTRL_U)

        h.run(driver)
        assert seen["rows"] == 3  # 200 characters after "> ", 78 per row
        assert seen["prefix"] == [("", "  ")]  # continuation rows line up with the text

    def test_the_input_never_takes_the_whole_screen(self):
        h = Harness(simple_runner)
        seen = {}

        def driver(h):
            h.send(SHIFT_TAB.join(["line"] * 30))
            wait_for(lambda: h.screen._buffer.text.count("\n") == 29)
            seen["rows"] = h.screen._input_rows()
            h.send(CTRL_C)  # empties the whole input (Ctrl+U only empties the current line)
            wait_for(lambda: h.screen._buffer.text == "")

        h.run(driver)
        assert seen["rows"] == 10

    def test_shift_tab_still_goes_back_in_a_completion_list(self):
        registry = SlashRegistry(Renderer(Console(file=io.StringIO(), width=80)))
        add_basic_commands(registry)
        registry.add(SlashCommand("model", "m", lambda a: CommandResult()))
        h = Harness(simple_runner, commands=registry)

        def driver(h):
            h.send("/")
            wait_for(lambda: h.screen._suggestion_lines())
            h.send("\t")
            wait_for(lambda: h.screen._buffer.text == "/help")
            h.send("\t")
            wait_for(lambda: h.screen._buffer.text == "/clear")
            h.send(SHIFT_TAB)
            wait_for(lambda: h.screen._buffer.text == "/help")
            h.send(CTRL_U)

        h.run(driver)


# --------------------------------------------------------------------------- #
# The agent's questions
# --------------------------------------------------------------------------- #

OPTIONS = [Choice("SQLite", "embedded, nothing to install"), Choice("PostgreSQL", "needs a server"), Choice("CSV")]


def choice_harness(multiple=False, allow_other=True, options=OPTIONS):
    answers, cancels, holder = [], [], {}

    def runner(view, cancel):
        answers.append(holder["h"].screen.ask_choice("Which database?", options, multiple, allow_other))
        cancels.append(cancel.is_set())

    holder["h"] = Harness(runner)
    return holder["h"], answers, cancels


def open_choice(h):
    h.send("go\r")
    wait_for(lambda: h.screen._choice is not None)


def answer_with(keys, **options):
    """Open a question, type `keys`, return what ask_choice gave."""
    h, answers, _ = choice_harness(**options)

    def driver(h):
        open_choice(h)
        h.send(keys)
        wait_for(lambda: answers and h.idle())

    h.run(driver)
    return answers[0]


class TestChoice:
    def test_the_question_its_options_and_the_hints(self):
        h, answers, _ = choice_harness()
        seen = {}

        def driver(h):
            open_choice(h)
            seen["lines"] = [text for _, text in h.screen._choice_lines(80)]
            seen["prompt"] = joined(h.screen._prompt_fragments())
            seen["header"] = joined(h.screen._header_fragments())
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        lines = seen["lines"]
        assert lines[0] == "? Which database?" and lines[1].startswith("❯ 1. SQLite")
        assert lines[2].strip() == "embedded, nothing to install" and lines[2].startswith("     ")
        assert any("Something else" in line for line in lines) and "Esc skip" in lines[-1]
        assert "answer >" in seen["prompt"] and "question" in seen["header"]
        assert answers == [Answer(["SQLite"])]

    def test_enter_picks_the_option_under_the_cursor(self):
        assert answer_with(DOWN + ENTER) == Answer(["PostgreSQL"])

    def test_several_options_can_be_ticked(self):
        h, answers, _ = choice_harness(multiple=True)
        seen = {}

        def driver(h):
            open_choice(h)
            h.send(SPACE + DOWN + DOWN + SPACE)
            wait_for(lambda: h.screen._choice.checked == {0, 2})
            seen["lines"] = [text for _, text in h.screen._choice_lines(80)]
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [Answer(["SQLite", "CSV"])]
        assert any(line.startswith("  [x] 1. SQLite") for line in seen["lines"])
        assert any(line.startswith("  [ ] 2. PostgreSQL") for line in seen["lines"])

    def test_enter_without_ticks_takes_the_option_under_the_cursor(self):
        assert answer_with(DOWN + ENTER, multiple=True) == Answer(["PostgreSQL"])

    def test_typing_gives_another_answer(self):
        assert answer_with("MongoDB maybe" + ENTER) == Answer([], "MongoDB maybe")

    def test_ticked_options_and_a_typed_answer_go_together(self):
        assert answer_with(SPACE + "and Redis" + ENTER, multiple=True) == Answer(["SQLite"], "and Redis")

    def test_moving_back_to_an_option_chooses_it_over_the_typed_text(self):
        assert answer_with("draft" + UP + ENTER) == Answer(["CSV"])

    def test_enter_on_an_empty_other_answer_waits(self):
        h, answers, _ = choice_harness()

        def driver(h):
            open_choice(h)
            h.send(DOWN * 5 + ENTER)  # the cursor stops on "something else"
            time.sleep(0.3)
            assert not answers and h.screen._choice.on_other()
            h.send(UP + ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [Answer(["CSV"])]

    def test_without_other_answers_typing_is_ignored(self):
        h, answers, _ = choice_harness(allow_other=False)
        seen = {}

        def driver(h):
            open_choice(h)
            h.send("xyz")
            time.sleep(0.3)
            seen["text"] = h.screen._buffer.text
            seen["lines"] = [text for _, text in h.screen._choice_lines(80)]
            h.send(DOWN * 5 + ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert seen["text"] == "" and not any("Something else" in line for line in seen["lines"])
        assert answers == [Answer(["CSV"])]

    @pytest.mark.parametrize("key, stops", [(ESCAPE, False), (CTRL_C, True)])
    def test_escape_skips_and_ctrl_c_also_stops_the_turn(self, key, stops):
        h, answers, cancels = choice_harness()

        def driver(h):
            open_choice(h)
            h.send(key)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [None] and cancels == [stops]

    def test_what_was_typed_before_the_question_comes_back(self):
        release = threading.Event()
        answers, holder = [], {}

        def runner(view, cancel):
            release.wait(5)
            answers.append(holder["h"].screen.ask_choice("Pick", OPTIONS))

        h = holder["h"] = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._busy)
            h.send("my next question")
            wait_for(lambda: h.screen._buffer.text == "my next question")
            release.set()
            wait_for(lambda: h.screen._choice is not None)
            seen["during"] = h.screen._buffer.text
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())
            seen["after"] = h.screen._buffer.text
            h.send(CTRL_U)

        h.run(driver)
        assert seen["during"] == "" and seen["after"] == "my next question"

    def test_a_question_without_options_is_a_free_answer(self):
        assert answer_with("free text" + ENTER, options=[]) == Answer([], "free text")

    def test_asking_outside_a_running_screen_fails_clearly(self):
        with create_pipe_input() as pipe:
            screen = AgentScreen(
                title="t", console=Console(file=io.StringIO()), turn_runner=simple_runner, input=pipe, output=DummyOutput()
            )
            with pytest.raises(RuntimeError, match="not running"):
                screen.ask_choice("x", OPTIONS)
            with pytest.raises(ValueError):
                screen.ask_choice("x", [], allow_other=False)


PAGE_DOWN, END = "\x1b[6~", "\x1b[F"


class TestLongChoices:
    def test_a_long_list_scrolls_and_starts_on_the_given_option(self):
        options = [Choice(f"model-{i:02d}", "a model") for i in range(60)]
        answers, holder = [], {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_choice("Model?", options, allow_other=False, initial=40))

        h = holder["h"] = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._choice is not None)
            seen["first"] = [text for _, text in h.screen._choice_lines(80)]
            h.send(PAGE_DOWN)
            wait_for(lambda: h.screen._choice.cursor > 40)
            seen["paged"] = h.screen._choice.cursor
            h.send(END)
            wait_for(lambda: h.screen._choice.cursor == 59)
            seen["last"] = [text for _, text in h.screen._choice_lines(80)]
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        first = seen["first"]
        assert len(first) <= 40 - 2  # fits the 40-row terminal with the input and the rule
        assert any(line.startswith("❯ 41. model-40") for line in first)
        assert re.search(r"↑ \d+ more", first[-1]) and re.search(r"↓ \d+ more", first[-1])
        assert seen["paged"] > 41
        assert any(line.startswith("❯ 60. model-59") for line in seen["last"]) and "more" in seen["last"][-1]
        assert not re.search(r"↓ \d+ more", seen["last"][-1])
        assert answers == [Answer(["model-59"])]


class TestSecretText:
    def test_a_key_is_typed_masked_and_kept_out_of_the_history(self):
        answers, holder = [], {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_text("API key?", secret=True))

        h = holder["h"] = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._text is not None)
            seen["prompt"] = joined(h.screen._prompt_fragments())
            seen["question"] = joined(h.screen._question_fragments())
            seen["header"] = joined(h.screen._header_fragments())
            h.send("sk-secret" + UP + DOWN)  # the arrows recall nothing into the secret
            wait_for(lambda: h.screen._buffer.text == "sk-secret")
            h.send(ENTER)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == ["sk-secret"] and "key >" in seen["prompt"] and "API key?" in seen["question"]
        assert "Esc: cancel" in seen["header"]
        assert "sk-secret" not in h.screen._buffer.history.get_strings()

    def test_escape_cancels(self):
        answers, holder = [], {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_text("Name?"))

        h = holder["h"] = Harness(runner)

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._text is not None)
            h.send("half")
            time.sleep(0.6)  # past the input's escape timeout
            h.send(ESCAPE)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [None] and h.screen._buffer.text == ""


# --------------------------------------------------------------------------- #
# Tool details and the transcript
# --------------------------------------------------------------------------- #


def tool_runner(view, cancel):
    note = view.tool_started("file_system_edit", {"path": "a.py"})
    view.tool_finished(note, "file_system_edit", {"path": "a.py"}, True, 0.1, summary="+1 −1", detail="-old\n+new", detail_kind="diff")
    view.add_text("Edited.")
    return TurnStats(1, 2, 3, 1.0)


class TestDetailsAndTranscript:
    def test_ctrl_o_shows_the_details_of_the_next_tool_lines(self):
        h = Harness(tool_runner)
        seen = {}

        def driver(h):
            h.send("first\r")
            wait_for(lambda: h.output.count("Edited.") == 1 and h.idle())
            h.send(CTRL_O)
            wait_for(lambda: h.screen._details)
            seen["header"] = joined(h.screen._header_fragments())
            h.send("second\r")
            wait_for(lambda: h.output.count("Edited.") == 2 and h.idle())

        out = h.run(driver)
        first, second = out.split("❯ second")
        assert "· +1 −1" in first and "+new" not in first
        assert "+new" in second and "-old" in second
        assert "tool details shown" in seen["header"]

    def test_the_transcript_opens_and_closes(self):
        h = Harness(tool_runner)
        seen = {}

        def viewer_ready():
            viewer = h.screen.viewer
            return viewer is not None and viewer.app.is_running

        def driver(h):
            h.send("hello\r")
            wait_for(lambda: "Edited." in h.output and h.idle())
            h.send(CTRL_T)
            wait_for(viewer_ready)
            time.sleep(0.3)  # the viewer takes the input over
            seen["turns"] = len(h.screen.viewer.turns)
            h.send("q")
            wait_for(lambda: h.idle() and h.screen.viewer is None)
            h.send("/transcript\r")
            wait_for(viewer_ready)
            # A lone Escape is only told apart from an escape sequence after a timeout (0.5s); the
            # screen and the viewer share the input, so the screen's own timeout must be over first.
            time.sleep(0.7)
            h.send(ESCAPE)
            wait_for(lambda: h.idle() and h.screen.viewer is None)
            seen["alive"] = h.screen._app.is_running

        h.run(driver)
        assert seen["alive"] and seen["turns"] == 1

    def test_an_empty_transcript_says_so(self):
        h = Harness(simple_runner)

        def driver(h):
            h.send("/transcript\r")
            wait_for(lambda: "Nothing to show yet." in h.output and h.idle())

        h.run(driver)
