from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from custom_console.agent.render import (
    TurnPrinter,
    live_lines,
    markdown_cut,
    render_turn,
    segment_renderable,
    stats_line,
    todo_lines,
    turn_renderables,
    wrap_line,
)
from custom_console.agent.results import ToolResult
from custom_console.agent.session import tool_display
from custom_console.agent.turn import (
    STYLE_ERROR,
    STYLE_TOOL,
    TurnStats,
    TurnView,
    format_arguments,
)


def render_to_text(view: TurnView, width: int = 80, details: bool = False) -> str:
    console = Console(file=io.StringIO(), width=width, force_terminal=False)
    console.print(render_turn(view, details))
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

    def test_stats_line_shows_the_context(self):
        assert stats_line(TurnStats(10, 20, 30, 1.5, context_percent=42.4)).endswith("· context 42%")


# --------------------------------------------------------------------------- #
# What tool lines hide, and the checklist
# --------------------------------------------------------------------------- #

DIFF = "@@ -1,2 +1,2 @@\n one\n-two\n+2\n… 3 more line(s)"


def edited(view, detail=DIFF, kind="diff"):
    note = view.tool_started("file_system_edit", {"path": "a.txt"})
    view.tool_finished(note, "file_system_edit", {"path": "a.txt"}, True, 0.1, summary="+1 −1", detail=detail, detail_kind=kind)
    return note


class TestToolDetails:
    def test_a_tool_line_carries_its_summary_and_hides_its_detail(self):
        view = TurnView("hi")
        edited(view)
        view.add_text("Done.")
        note = view.snapshot()[0]
        assert note.text == "✔ file_system_edit(path='a.txt') · 0.1s · +1 −1"
        assert note.detail == DIFF and note.done
        assert [s.kind for s in view.snapshot()] == ["note", "text"] and view.answer_text() == "Done."

    def test_a_running_tool_is_not_done_and_can_collect_detail_lines(self):
        view = TurnView("hi")
        note = view.tool_started("task", {})
        view.add_detail(note, "✔ file_system_read() · 0.1s")
        view.add_detail(note, "✔ file_system_grep() · 0.2s")
        assert not view.snapshot()[0].done
        assert view.snapshot()[0].detail.splitlines() == ["✔ file_system_read() · 0.1s", "✔ file_system_grep() · 0.2s"]

    def test_details_are_hidden_unless_asked_for(self):
        view = TurnView("q")
        edited(view)
        assert [text for _, text in live_lines(view, 80, 20)][1:] == ["✔ file_system_edit(path='a.txt') · 0.1s · +1 −1"]
        assert "-two" not in render_to_text(view)
        assert "-two" in render_to_text(view, details=True)

    def test_diff_details_are_styled_by_their_first_character(self):
        view = TurnView("q")
        edited(view)
        printer = TurnPrinter(details=True)
        styled = dict((text.strip(), style) for style, text in live_lines(view, 80, 20, printer)[2:])
        assert styled["-two"] == "class:diff-del" and styled["+2"] == "class:diff-add"
        assert styled["@@ -1,2 +1,2 @@"] == "class:diff-hunk" and styled["one"] == "class:diff-ctx"
        assert styled["… 3 more line(s)"] == "class:note"

    def test_plain_details_and_long_lines_stay_inside_the_width(self):
        view = TurnView("q")
        edited(view, "word " * 40, "")
        lines = live_lines(view, 30, 100, TurnPrinter(details=True))
        assert all(len(text) <= 30 for _, text in lines) and lines[-1][0] == "class:detail"

    def test_diff_colours_in_the_final_rendering(self):
        from custom_console.agent.turn import Segment

        rendered = segment_renderable(Segment("note", "✔ edit", "tool", "-old\n+new\n ctx", "diff"), details=True)
        hidden = rendered.renderables[1]
        spans = {hidden.plain[s.start : s.end].strip(): str(s.style) for s in hidden.spans}
        assert spans["-old"] == "red" and spans["+new"] == "green"

    def test_details_are_not_processed_as_markdown(self):
        view = TurnView("q")
        view.add_text("Before.")
        edited(view, "-# not a heading\n+**not bold**")
        view.add_text("After.")
        text = render_to_text(view, details=True)
        assert "-# not a heading" in text and "+**not bold**" in text
        assert text.index("Before.") < text.index("-# not a heading") < text.index("After.")

    def test_diffs_of_older_sessions_are_details_too(self):
        view = TurnView.from_dict({"prompt": "q", "segments": [{"kind": "diff", "text": "-a\n+b"}]})
        assert "+b" not in render_to_text(view) and "+b" in render_to_text(view, details=True)


class TestChecklist:
    def test_the_checklist_is_kept_apart_from_the_stream(self):
        view = TurnView("hi")
        view.show_todos("☐ a\n☐ b")
        view.add_text("working")
        view.show_todos("☑ a\n◐ b")
        assert view.todos == "☑ a\n◐ b" and [s.kind for s in view.snapshot()] == ["text"]

    def test_its_last_state_is_printed_at_the_end_of_the_turn(self):
        view = TurnView("q")
        view.add_text("Answer.")
        view.show_todos("☑ done\n◐ doing")
        view.stats = TurnStats(1, 2, 3, 1.0)
        text = render_to_text(view)
        assert text.index("Answer.") < text.index("☑ done") < text.index("◐ doing") < text.index("tokens")

    def test_an_empty_checklist_prints_nothing(self):
        view = TurnView("q")
        view.show_todos("☐ a")
        view.show_todos("")
        assert "☐" not in render_to_text(view)

    def test_checklist_lines_are_styled_by_state(self):
        styles = [style for style, _ in todo_lines("☑ done\n◐ doing\n☐ later", 80)]
        assert styles == ["class:todo-done", "class:todo-active", "class:todo"]


# --------------------------------------------------------------------------- #
# Printing a turn while it streams
# --------------------------------------------------------------------------- #


class TestMarkdownCut:
    @pytest.mark.parametrize(
        "text",
        [
            "one paragraph still being written",
            "para\n\n",  # the next block has not started
            "para\n\nab",  # too early to tell a list item from a paragraph
            "intro:\n\n- item",  # a list may continue the block above
            "code:\n\n    indented",
            "```\ncode\n\nmore code\n",  # inside a fence
        ],
    )
    def test_nothing_is_final_yet(self, text):
        assert markdown_cut(text) == 0

    def test_a_paragraph_is_final_once_the_next_one_starts(self):
        text = "First paragraph.\n\nSecond one is coming"
        assert text[: markdown_cut(text)] == "First paragraph.\n\n"

    def test_the_last_finished_block_is_the_cut(self):
        text = "# Title\n\nOne.\n\nTwo.\n\nThree"
        assert text[: markdown_cut(text)] == "# Title\n\nOne.\n\nTwo.\n\n"

    def test_a_closed_fence_is_final(self):
        text = "```py\nx = 1\n\ny = 2\n```\n\nAfter the code"
        assert text[: markdown_cut(text)] == "```py\nx = 1\n\ny = 2\n```\n\n"

    def test_a_list_is_final_once_a_paragraph_follows(self):
        text = "- a\n\n- b\n\nThat is all"
        assert text[: markdown_cut(text)] == "- a\n\n- b\n\n"


def printed(parts, width=80) -> str:
    console = Console(file=io.StringIO(), width=width, force_terminal=False)
    for part in parts:
        console.print(part)
    return console.file.getvalue()


class TestTurnPrinter:
    def test_the_prompt_comes_first_then_only_finished_blocks(self):
        view, printer = TurnView("question"), TurnPrinter()
        assert printed(printer.take(view)).strip() == "❯ question"
        view.add_text("First paragraph.\n\nSecond")
        assert printed(printer.take(view)).strip() == "First paragraph."
        assert printer.take(view) == []  # nothing new
        assert [text for _, text in live_lines(view, 80, 20, printer)] == ["Second"]

    def test_a_running_tool_holds_back_what_follows(self):
        view, printer = TurnView("q"), TurnPrinter()
        printer.take(view)
        view.add_text("Let me look.")
        note = view.tool_started("file_system_list", {})
        assert "Let me look." in printed(printer.take(view))
        assert printer.take(view) == []  # the tool line still changes
        assert [text for _, text in live_lines(view, 80, 20, printer)] == ["▸ file_system_list() …"]
        view.tool_finished(note, "file_system_list", {}, True, 0.1)
        assert "✔ file_system_list()" in printed(printer.take(view))
        assert live_lines(view, 80, 20, printer) == []

    def test_printing_in_pieces_gives_the_same_text_as_printing_at_once(self):
        view, printer = TurnView("q"), TurnPrinter()
        pieces = [printer.take(view)]
        for chunk in ("# Title\n\nSome ", "**bold** text.\n\n", "- one\n- two\n\nEnd"):
            view.add_text(chunk)
            pieces.append(printer.take(view))
        note = view.tool_started("t", {})
        pieces.append(printer.take(view))
        view.tool_finished(note, "t", {}, True, 0.1)
        view.add_text("After the tool.")
        pieces.append(printer.take(view))
        view.stats = TurnStats(1, 2, 3, 1.0)
        pieces.append(printer.take(view, final=True))
        assert printed([part for piece in pieces for part in piece]) == render_to_text(view)

    def test_has_news_does_not_move_the_position(self):
        view, printer = TurnView("q"), TurnPrinter()
        assert printer.has_news(view) and printer.has_news(view)
        printer.take(view)
        assert not printer.has_news(view)
        view.add_text("a\n\nb is coming")
        assert printer.has_news(view)

    def test_details_apply_from_the_moment_they_are_switched_on(self):
        view, printer = TurnView("q"), TurnPrinter()
        edited(view)
        assert "-two" not in printed(printer.take(view))
        printer.details = True
        edited(view)
        assert "-two" in printed(printer.take(view))


class TestTokenCount:
    def test_chunks_are_counted_until_the_request_reports_its_tokens(self):
        view = TurnView("q")
        view.add_text("a")
        view.add_text("b")
        assert view.output_tokens == 2
        view.request_finished(7)
        assert view.output_tokens == 7
        view.add_text("c")
        view.count_chunk()
        assert view.output_tokens == 9
        view.request_finished(None)  # no count reported: the chunks stand
        assert view.output_tokens == 9

    def test_elapsed_is_the_duration_once_the_turn_is_over(self):
        view = TurnView("q")
        assert view.elapsed >= 0
        view.stats = TurnStats(1, 2, 3, 4.5)
        assert view.elapsed == 4.5


class TestToolDisplay:
    def test_a_diff_gives_the_counts_and_is_the_detail(self):
        result = ToolResult.ok({"lines_added": 12, "lines_removed": 3}, diff="+a\n-b")
        assert tool_display(result) == ("+12 −3", "+a\n-b", "diff")

    def test_a_short_result_is_shown_on_the_line_and_nothing_is_hidden(self):
        assert tool_display(ToolResult.ok("No match.")) == ("No match.", "", "")

    def test_a_long_result_is_hidden_and_counted(self):
        summary, detail, kind = tool_display(ToolResult.ok("\n".join(f"line {i}" for i in range(5))))
        assert summary == "5 lines" and detail.endswith("line 4") and kind == ""

    def test_a_command_shows_its_output_and_data_is_json_otherwise(self):
        assert tool_display(ToolResult.ok({"exit_code": 0, "output": "a\nb"}))[1] == "a\nb"
        assert '"x": 1' in tool_display(ToolResult.ok({"x": 1, "y": [1, 2]}))[1]

    def test_a_failure_keeps_its_partial_output_and_explicit_values_win(self):
        assert tool_display(ToolResult.fail(RuntimeError("exit 1"), {"output": "boom"})) == ("", "boom", "")
        assert tool_display(ToolResult.ok("x", summary="→ yes", detail="Q?\n→ yes")) == ("→ yes", "Q?\n→ yes", "")

    def test_huge_details_are_clipped(self):
        detail = tool_display(ToolResult.ok("\n".join("x" for _ in range(1000))))[1]
        assert detail.splitlines()[-1] == "… 600 more line(s)"

