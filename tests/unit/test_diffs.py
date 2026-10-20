"""Diffs and previews are bounded (N4), and the closest-passage hint has a budget (PERF-2)."""

from __future__ import annotations

from custom_console.core import diffs


def test_a_long_diff_is_cut_with_the_number_of_lines_left_out():
    old = "\n".join(f"line {i}" for i in range(300))
    new = "\n".join(f"changed {i}" for i in range(300))
    shown = diffs.unified(old, new, "big.txt").split("\n")
    assert len(shown) == diffs.MAX_PREVIEW_ROWS + 1
    assert shown[-1].startswith("... ") and shown[-1].endswith(" more lines") and int(shown[-1].split()[1]) > 500


def test_a_short_diff_is_shown_whole():
    shown = diffs.unified("a\nb\n", "a\nc\n", "f.txt")
    assert "-b" in shown and "+c" in shown and "more lines" not in shown and shown.startswith("--- a/f.txt")


def test_a_new_file_preview_is_cut_too():
    text = "\n".join(f"row {i}" for i in range(200)) + "\n"
    shown = diffs.new_file_preview(text).split("\n")
    assert shown[0] == "+row 0" and shown[-1] == "... 160 more lines" and len(shown) == diffs.MAX_PREVIEW_ROWS + 1


def test_the_stats_count_added_and_removed_lines():
    assert diffs.stats("a\nb\nc", "a\nx\nc\nd") == (2, 1)
