from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from custom_console.agent.render import (
    live_lines,
    render_turn,
    stats_line,
    turn_renderables,
    wrap_line,
)
from custom_console.agent.turn import (
    STYLE_ERROR,
    STYLE_TOOL,
    TurnStats,
    TurnView,
    format_arguments,
)


def render_to_text(view: TurnView, width: int = 80) -> str:
    console = Console(file=io.StringIO(), width=width, force_terminal=False)
    console.print(render_turn(view))
    return console.file.getvalue()


# --------------------------------------------------------------------------- #
# TurnView / TurnStats
# --------------------------------------------------------------------------- #


class TestTurnView:
    def test_consecutive_chunks_form_one_text_segment(self):
        view = TurnView("hi")
        view.add_text("Hel")
        view.add_text("lo")
        assert [(s.kind, s.text) for s in view.snapshot()] == [("text", "Hello")]

    def test_empty_chunks_are_ignored(self):
        view = TurnView("hi")
        view.add_text("")
        assert view.snapshot() == []

    def test_a_note_splits_the_text(self):
        view = TurnView("hi")
        view.add_text("before")
        view.add_note("a note")
        view.add_text("after")
        assert [(s.kind, s.text) for s in view.snapshot()] == [("text", "before"), ("note", "a note"), ("text", "after")]
        assert view.answer_text() == "beforeafter"

    def test_tool_lifecycle_updates_the_same_line_in_place(self):
        view = TurnView("hi")
        index = view.tool_started("file_system_list", {"path": "."})
        assert view.activity == "running file_system_list"
        assert view.snapshot()[0].text == "▸ file_system_list(path='.') …"

        view.tool_finished(index, "file_system_list", {"path": "."}, True, 0.26)
        note = view.snapshot()[0]
        assert len(view.snapshot()) == 1 and note.style == STYLE_TOOL
        assert note.text == "✔ file_system_list(path='.') · 0.3s"
        assert view.activity == "thinking"

    def test_failed_tool_shows_the_error(self):
        view = TurnView("hi")
        index = view.tool_started("t", {})
        view.tool_finished(index, "t", {}, False, 1.0, "boom")
        note = view.snapshot()[0]
        assert note.style == STYLE_ERROR and note.text.startswith("✘ t() · 1.0s") and note.text.endswith("— boom")

    def test_permission_note(self):
        view = TurnView("hi")
        view.permission("Agent wants to x.", "refused")
        assert view.snapshot()[0].text == "? Agent wants to x. → refused"

    def test_permission_is_shown_before_the_tool_call_it_authorizes(self):
        view = TurnView("hi")
        view.add_text("Let me look.")
        note = view.tool_started("file_system_copy", {"src": "a"})
        view.permission("Agent wants to copy a.", "accepted")
        view.tool_finished(note, "file_system_copy", {"src": "a"}, True, 0.1)

        texts = [s.text for s in view.snapshot()]
        assert texts[1] == "? Agent wants to copy a. → accepted"
        assert texts[2].startswith("✔ file_system_copy(src='a')")  # the tool line was updated in place

    def test_permission_goes_before_the_right_tool_when_several_ran(self):
        view = TurnView("hi")
        first = view.tool_started("a", {})
        view.tool_finished(first, "a", {}, True, 0.1)
        second = view.tool_started("b", {})
        view.permission("for b", "accepted")
        view.tool_finished(second, "b", {}, True, 0.1)
        assert [s.text.split("(")[0] for s in view.snapshot()] == ["✔ a", "? for b → accepted", "✔ b"]

    def test_identical_looking_notes_do_not_get_confused(self):
        view = TurnView("hi")
        first = view.tool_started("t", {})
        second = view.tool_started("t", {})
        view.tool_finished(second, "t", {}, False, 1.0, "bad")
        assert first.text.startswith("▸") and second.text.startswith("✘")

    def test_snapshot_is_a_copy(self):
        view = TurnView("hi")
        view.add_text("a")
        snapshot = view.snapshot()
        view.add_text("b")
        assert snapshot[0].text == "a"

    def test_answer_excludes_notes(self):
        view = TurnView("hi")
        view.add_note("x")
        assert view.answer_text() == ""


def test_format_arguments_shortens():
    assert format_arguments({}) == ""
    assert format_arguments({"a": 1, "b": "x"}) == "a=1, b='x'"
    long = format_arguments({"content": "y" * 500})
    assert len(long) <= 100 and "…" in long


class TestTurnStats:
    def test_from_metrics_tolerates_missing_values(self):
        stats = TurnStats.from_metrics(SimpleNamespace(input_tokens=5, output_tokens=None), 2.0)
        assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (5, 0, 0)

    def test_speed(self):
        assert TurnStats(0, 50, 50, 2.0).tokens_per_second == 25.0
        assert TurnStats(0, 50, 50, 0.0).tokens_per_second == 0.0

    def test_stats_line(self):
        assert stats_line(TurnStats(10, 20, 30, 1.5)) == "10 prompt + 20 completion = 30 tokens · 1.5s · 13.3 tokens/s"


# --------------------------------------------------------------------------- #
# Wrapping / live rendering
# --------------------------------------------------------------------------- #


class TestWrapLine:
    def test_short_line_is_untouched(self):
        assert wrap_line("hello", 10) == ["hello"]
        assert wrap_line("", 10) == [""]

    def test_breaks_at_spaces(self):
        assert wrap_line("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]

    def test_hard_break_inside_a_long_word(self):
        assert wrap_line("abcdefghij", 4) == ["abcd", "efgh", "ij"]

    def test_every_line_fits_the_width(self):
        text = "The quick brown fox jumps over the lazy dog " * 5
        assert all(len(part) <= 20 for part in wrap_line(text, 20))

    def test_nothing_is_lost(self):
        text = "word " * 30
        assert " ".join(wrap_line(text, 17)).split() == text.split()

    def test_wide_characters_count_double(self):
        assert all(len(part) <= 3 for part in wrap_line("日本語日本語", 6))
        assert wrap_line("日本語日本語", 6) == ["日本語", "日本語"]

    def test_tabs_and_carriage_returns(self):
        assert wrap_line("a\tb\r", 20) == ["a    b"]

    def test_non_positive_width_is_safe(self):
        assert wrap_line("abc", 0) == ["abc"]


class TestLiveLines:
    def test_prompt_comes_first_then_text_then_notes(self):
        view = TurnView("question")
        view.add_text("line one\nline two")
        view.add_note("▸ tool()", STYLE_TOOL)
        lines = live_lines(view, 80, 20)
        assert lines == [
            ("class:prompt", "❯ question"),
            ("", "line one"),
            ("", "line two"),
            ("class:tool", "▸ tool()"),
        ]

    def test_markdown_is_left_as_plain_text(self):
        view = TurnView("q")
        view.add_text("# Title\n**bold**")
        assert [text for _, text in live_lines(view, 80, 20)][1:] == ["# Title", "**bold**"]

    def test_only_the_tail_is_kept_when_too_tall(self):
        view = TurnView("q")
        view.add_text("\n".join(f"row {i}" for i in range(50)))
        lines = live_lines(view, 80, 6)
        assert len(lines) == 6
        assert lines[0] == ("class:note", "…") and lines[-1] == ("", "row 49")

    def test_long_lines_are_wrapped_to_the_width(self):
        view = TurnView("q")
        view.add_text("word " * 40)
        assert all(len(text) <= 30 for _, text in live_lines(view, 30, 100))

    def test_a_single_row_budget(self):
        view = TurnView("q")
        view.add_text("a\nb\nc")
        assert live_lines(view, 80, 1) == [("", "c")]

    def test_no_output_yet_still_shows_the_prompt(self):
        assert live_lines(TurnView("hello"), 80, 10) == [("class:prompt", "❯ hello")]


# --------------------------------------------------------------------------- #
# Final (Markdown) rendering
# --------------------------------------------------------------------------- #


class TestFinalRendering:
    def test_markdown_is_rendered(self):
        view = TurnView("hi")
        view.add_text("# Title\n\nSome **bold** text and `code`.\n\n- one\n- two\n")
        text = render_to_text(view)
        assert "Title" in text and "bold" in text and "code" in text
        assert "**" not in text and "# Title" not in text and "`" not in text
        assert "•" in text  # list bullets

    def test_user_prompt_is_repeated_first(self):
        view = TurnView("what time is it")
        view.add_text("noon")
        assert render_to_text(view).lstrip().startswith("❯ what time is it")

    def test_notes_and_text_keep_their_order(self):
        view = TurnView("q")
        view.add_text("First.")
        view.add_note("✔ tool() · 0.1s", STYLE_TOOL)
        view.add_text("Second.")
        text = render_to_text(view)
        assert text.index("First.") < text.index("✔ tool()") < text.index("Second.")

    def test_stats_footer(self):
        view = TurnView("q")
        view.add_text("a")
        view.stats = TurnStats(1, 2, 3, 1.0)
        assert "1 prompt + 2 completion = 3 tokens" in render_to_text(view)

    def test_no_stats_no_footer(self):
        view = TurnView("q")
        view.add_text("a")
        assert "tokens" not in render_to_text(view)

    def test_whitespace_only_text_is_dropped(self):
        view = TurnView("q")
        view.add_text("  \n ")
        view.add_note("note")
        assert len([r for r in turn_renderables(view) if type(r).__name__ == "Markdown"]) == 0

    def test_trailing_notes_are_still_shown(self):
        view = TurnView("q")
        view.add_text("answer")
        view.add_note("Interrupted by the user.")
        assert render_to_text(view).rstrip().endswith("Interrupted by the user.")

    def test_rich_markup_in_model_output_is_not_interpreted(self):
        view = TurnView("[bold]q[/bold]")
        view.add_text("a [red]tag[/red] stays")
        text = render_to_text(view)
        assert "[bold]q[/bold]" in text and "[red]tag[/red]" in text
