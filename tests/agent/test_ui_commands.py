"""AgentScreen: slash commands, completion list, layout height, default-no questions."""

from __future__ import annotations

import io
import threading

import pytest
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from rich.console import Console

from custom_console.agent.slash import CommandResult, Renderer, SlashCommand, SlashCompleter, SlashRegistry
from custom_console.agent.turn import TurnStats
from custom_console.agent.ui import add_basic_commands, parse_answer
from test_ui import Harness, joined, simple_runner, wait_for


def registry_with(*commands):
    registry = SlashRegistry(Renderer(Console(file=io.StringIO(), width=80)))
    add_basic_commands(registry)
    for command in commands:
        registry.add(command)
    return registry


def model_command(handler=None):
    def complete(arguments):
        for name in ("alpha:1b", "beta:2b", "alpine:3b"):
            if name.startswith(arguments):
                yield Completion(name, start_position=-len(arguments), display_meta="local")

    return SlashCommand("model", "switch model", handler or (lambda a: CommandResult(f"switched to {a}\n")), "[MODEL]", complete)


# --------------------------------------------------------------------------- #
# Registry and completer
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_run_passes_the_arguments_and_unknown_commands_are_reported(self):
        registry = registry_with(model_command())
        assert registry.run("/model   big:1b  ").text == "switched to big:1b\n"
        assert "Unknown command: /nope" in registry.run("/nope").text

    def test_lookup_is_case_insensitive_and_aliases_work(self):
        registry = registry_with()
        assert registry.run("/BYE").quit
        assert registry.run("/exit").quit and registry.run("/quit").quit

    def test_aliases_are_not_listed_twice(self):
        names = [c.name for c in registry_with().commands()]
        assert names == ["help", "clear", "transcript", "bye"]

    def test_help_groups_commands_and_mentions_the_keys(self):
        registry = registry_with(SlashCommand("ls", "list", lambda a: CommandResult(), "[PATH]", group="Files"), model_command())
        text = registry.run("/help").text
        assert "Agent:" in text and "Files:" in text and "/model [MODEL]" in text and "/ls [PATH]" in text and "Ctrl+C" in text


class TestCompleter:
    def complete(self, registry, text):
        return list(SlashCompleter(registry).get_completions(Document(text), None))

    def test_command_names_with_their_summary(self):
        completions = self.complete(registry_with(model_command()), "/m")
        assert [(c.text, c.display_text, c.display_meta_text) for c in completions] == [("/model ", "/model", "switch model")]
        assert completions[0].start_position == -2  # replaces what was typed, slash included

    def test_a_bare_slash_lists_everything(self):
        assert {c.display_text for c in self.complete(registry_with(model_command()), "/")} == {"/help", "/clear", "/transcript", "/bye", "/model"}

    def test_commands_without_arguments_get_no_trailing_space(self):
        assert self.complete(registry_with(), "/he")[0].text == "/help"

    def test_arguments_are_completed_by_the_command(self):
        completions = self.complete(registry_with(model_command()), "/model al")
        assert [c.text for c in completions] == ["alpha:1b", "alpine:3b"]
        assert completions[0].start_position == -2

    def test_plain_text_and_unknown_commands_complete_nothing(self):
        registry = registry_with(model_command())
        assert self.complete(registry, "hello") == [] and self.complete(registry, "/zzz arg") == []


# --------------------------------------------------------------------------- #
# Screen behaviour
# --------------------------------------------------------------------------- #


class TestCommandsInTheScreen:
    def test_output_is_printed_above_and_the_agent_is_not_called(self):
        calls = []
        h = Harness(lambda v, c: calls.append(v.prompt), commands=registry_with(model_command()))

        def driver(h):
            h.send("/model beta:2b\r")
            wait_for(lambda: "switched to beta:2b" in h.output)
            wait_for(h.idle)

        h.run(driver)
        assert calls == []

    def test_a_command_can_hand_a_prompt_to_the_agent(self):
        prompts = []

        def runner(view, cancel):
            prompts.append(view.prompt)
            view.add_text("answered")
            return TurnStats(1, 1, 2, 0.1)

        registry = registry_with(SlashCommand("init", "make notes", lambda a: CommandResult(prompt="please explore")))
        h = Harness(runner, commands=registry)

        def driver(h):
            h.send("/init\r")
            wait_for(lambda: prompts and "answered" in h.output and h.idle())

        h.run(driver)
        assert prompts == ["please explore"]

    def test_a_failing_command_is_reported_and_the_screen_survives(self):
        def boom(arguments):
            raise RuntimeError("kaboom")

        h = Harness(simple_runner, commands=registry_with(SlashCommand("boom", "fails", boom)))

        def driver(h):
            h.send("/boom\r")
            wait_for(lambda: "/boom: RuntimeError: kaboom" in h.output)
            wait_for(h.idle)
            h.send("hello\r")
            wait_for(lambda: "tokens" in h.output and h.idle())

        h.run(driver)

    def test_a_slow_command_shows_its_name_in_the_header_and_blocks_a_second_one(self):
        release, started = threading.Event(), threading.Event()

        def slow(arguments):
            started.set()
            release.wait(5)
            return CommandResult("finished slow\n")

        h = Harness(simple_runner, commands=registry_with(SlashCommand("slow", "takes time", slow)))
        seen = {}

        def driver(h):
            h.send("/slow\r")
            assert started.wait(5)
            seen["header"] = joined(h.screen._header_fragments())
            h.send("/help\r")  # sent while busy: queued, run once /slow is done
            wait_for(lambda: h.screen._queue == ["/help"])
            release.set()
            wait_for(lambda: "finished slow" in h.output and "Keys:" in h.output and h.idle())

        h.run(driver)
        assert "running /slow" in seen["header"]

    def test_ctrl_c_during_a_command_does_not_claim_to_stop_it(self):
        release, started = threading.Event(), threading.Event()

        def slow(arguments):
            started.set()
            release.wait(5)
            return CommandResult("finished\n")

        h = Harness(simple_runner, commands=registry_with(SlashCommand("slow", "takes time", slow)))
        seen = {}

        def driver(h):
            h.send("/slow\r")
            assert started.wait(5)
            h.send("\x03")
            wait_for(lambda: True)
            seen["header"] = joined(h.screen._header_fragments())
            seen["cancelled"] = h.screen._cancel.is_set()
            release.set()
            wait_for(lambda: "finished" in h.output and h.idle())

        h.run(driver)
        assert "stopping" not in seen["header"] and seen["cancelled"] is False

    def test_quit_and_clear_results(self):
        h = Harness(simple_runner, banner="BANNER-TEXT", commands=registry_with())

        def driver(h):
            h.send("/clear\r")
            wait_for(lambda: h.output.count("BANNER-TEXT") == 2)

        h.run(driver)  # the harness ends with /bye, which must quit the loop

    def test_typing_a_slash_lists_suggestions_above_the_input(self):
        h = Harness(simple_runner, commands=registry_with(model_command()))
        seen = {}

        def driver(h):
            h.send("/mo")
            wait_for(lambda: h.screen._suggestion_lines())
            seen["rows"] = h.screen._suggestion_lines()
            seen["fragments"] = joined(h.screen._suggestion_fragments())
            h.send("\x15")  # Ctrl+U

        h.run(driver)
        assert [style for style, _ in seen["rows"]] == ["class:suggestion"]
        assert "/model" in seen["fragments"] and "switch model" in seen["fragments"]

    def test_tab_completes_and_cycles_through_arguments(self):
        h = Harness(simple_runner, commands=registry_with(model_command()))
        seen = []

        def driver(h):
            h.send("/mod")
            wait_for(lambda: h.screen._suggestion_lines())
            h.send("\t")  # completes the command name
            wait_for(lambda: h.screen._buffer.text == "/model ")
            h.send("al")
            wait_for(lambda: len(h.screen._suggestion_lines()) == 2)
            h.send("\t")
            wait_for(lambda: h.screen._buffer.text == "/model alpha:1b")
            seen.append([s for s, _ in h.screen._suggestion_lines()])
            h.send("\t")
            wait_for(lambda: h.screen._buffer.text == "/model alpine:3b")
            h.send("\x1b[Z")  # Shift+Tab goes back
            wait_for(lambda: h.screen._buffer.text == "/model alpha:1b")
            h.send("\x15")

        h.run(driver)
        assert seen == [["class:suggestion-selected", "class:suggestion"]]

    def test_no_suggestions_for_plain_text_or_while_a_question_is_asked(self):
        answers = []
        holder = {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_permission("ok?"))

        h = holder["h"] = Harness(runner, commands=registry_with(model_command()))

        def driver(h):
            h.send("hello")
            wait_for(lambda: h.screen._buffer.text == "hello")
            assert h.screen._suggestion_lines() == []
            h.send("\x15/mo")
            wait_for(lambda: h.screen._suggestion_lines())
            h.send("\x15")
            h.send("go\r")
            wait_for(lambda: h.screen._question is not None)
            assert h.screen._suggestion_lines() == []
            h.send("y\r")
            wait_for(lambda: answers and h.idle())

        h.run(driver)


class TestQuestionDefaults:
    @pytest.mark.parametrize(
        "text, default, expected",
        [("", True, True), ("", False, False), ("  ", False, False), ("y", False, True), ("n", True, False), ("oui", False, True), ("x", True, False)],
    )
    def test_parse_answer(self, text, default, expected):
        assert parse_answer(text, default) is expected

    @pytest.mark.parametrize("typed, expected", [("\r", False), ("y\r", True), ("n\r", False)])
    def test_a_default_no_question_needs_an_explicit_yes(self, typed, expected):
        answers, holder, seen = [], {}, {}

        def runner(view, cancel):
            answers.append(holder["h"].screen.ask_permission("Delete everything?", default=False))

        h = holder["h"] = Harness(runner)

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._question is not None)
            seen["prompt"] = joined(h.screen._prompt_fragments())
            h.send(typed)
            wait_for(lambda: answers and h.idle())

        h.run(driver)
        assert answers == [expected] and "(y|N)" in seen["prompt"]


# --------------------------------------------------------------------------- #
# Layout height
# --------------------------------------------------------------------------- #


class TestLayoutNeverShrinks:
    def test_the_filler_keeps_the_height_a_question_made_necessary(self):
        release, started = threading.Event(), threading.Event()
        holder = {}

        def runner(view, cancel):
            view.add_text("a line")
            holder["h"].screen.ask_permission("First line of the question.\nSecond line.\nThird line.")
            started.set()
            release.wait(5)

        h = holder["h"] = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: h.screen._question is not None)
            h.screen._filler_rows()  # one render while the question is on screen
            seen["asked_peak"] = h.screen._peak
            seen["asked_filler"] = h.screen._filler_rows()
            h.send("y\r")
            assert started.wait(5)
            wait_for(lambda: h.screen._question is None)
            seen["after_filler"] = h.screen._filler_rows()
            release.set()
            wait_for(h.idle)
            seen["idle_peak"] = h.screen._peak

        h.run(driver)
        assert seen["asked_filler"] == 0 and seen["asked_peak"] >= 4
        assert seen["after_filler"] == 3  # the three question rows are kept as blank rows
        assert seen["idle_peak"] < seen["asked_peak"]  # the printed turn took the place of filler rows

    def test_printed_text_takes_the_place_of_filler_rows(self):
        release = threading.Event()

        def runner(view, cancel):
            view.add_text("\n".join(f"row {i}" for i in range(6)))
            release.wait(5)

        h = Harness(runner)
        seen = {}

        def driver(h):
            h.send("go\r")
            wait_for(lambda: "❯ go" in h.output)  # the prompt is printed as soon as the turn starts
            wait_for(lambda: h.screen._filler_rows() >= 0 and h.screen._peak >= 6)
            seen["peak"], seen["printed"] = h.screen._peak, len(h.output)
            release.set()
            wait_for(h.idle)
            seen["after"] = h.screen._peak

        out = h.run(driver)
        rows = out[seen["printed"] :].count("\n")
        # No blank rows go to the scrollback: the filler keeps the input on the last row instead.
        assert seen["peak"] >= 6 and seen["after"] == max(0, seen["peak"] - rows)
        assert not out.rstrip(" ").endswith("\n\n\n")

    def test_a_suggestion_list_that_disappears_leaves_its_height_as_blank_space(self):
        h = Harness(simple_runner, commands=registry_with(model_command()))
        seen = {}

        def driver(h):
            h.send("/")
            wait_for(lambda: h.screen._suggestion_lines())
            h.screen._filler_rows()
            seen["peak"] = h.screen._peak
            h.send("\x15")
            wait_for(lambda: not h.screen._suggestion_lines())
            seen["filler"] = h.screen._filler_rows()

        h.run(driver)
        assert seen["peak"] >= 3 and seen["filler"] == seen["peak"]
