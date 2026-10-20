from __future__ import annotations

import pytest

from custom_console.core.vfs.backend import Stat
from custom_console.core.vfs.guard import OverwriteGuard
from custom_console.core.vfs.text import (
    compile_glob,
    count_lines,
    decode_text,
    expand_braces,
    glob_match,
    line_window,
    looks_binary,
    number_lines,
    split_lines,
)


@pytest.mark.parametrize(("text", "lines"), [
    ("", []), ("\n", [""]), ("a", ["a"]), ("a\n", ["a"]), ("a\nb", ["a", "b"]), ("a\nb\n", ["a", "b"]),
    ("a\n\nb\n", ["a", "", "b"]), ("\n\n", ["", ""]),
])
def test_one_way_to_split_lines(text, lines):
    assert split_lines(text) == lines and count_lines(text) == len(lines)


def test_decoding_normalises_line_ends_and_drops_the_bom():
    assert decode_text(b"\xef\xbb\xbfa\r\nb\rc\n") == "a\nb\nc\n"
    assert "�" in decode_text(b"bad \xff byte")
    assert looks_binary(b"abc\x00def") and not looks_binary("héllo".encode())
    assert not looks_binary(b"x" * 9000 + b"\x00")  # only the start is looked at


def test_numbered_lines_and_windows_agree():
    text = "one\ntwo\nthree\nfour\nfive\n"
    window = line_window(text, offset=2, limit=2)
    assert (window.lines, window.first, window.last, window.total, window.more_after) == (["two", "three"], 2, 3, 5, 2)
    assert number_lines(window.lines, window.first) == "2\ttwo\n3\tthree"
    assert number_lines(["a", "b"], 9) == " 9\ta\n10\tb"
    whole = line_window(text)
    assert whole.whole and whole.first == 1 and whole.last == 5
    assert line_window("", 1).total == 0 and line_window("", 1).first == 0
    assert line_window(text, offset=99).lines == [] and line_window(text, offset=0).first == 1


def test_brace_expansion():
    assert expand_braces("a.{py,md}") == ["a.py", "a.md"]
    assert expand_braces("{a,b}/{c,d}") == ["a/c", "b/c", "a/d", "b/d"] or sorted(expand_braces("{a,b}/{c,d}")) == ["a/c", "a/d", "b/c", "b/d"]
    assert sorted(expand_braces("x.{a,{b,c}}")) == ["x.a", "x.b", "x.c"]
    assert expand_braces("{single}") == ["{single}"] and expand_braces("plain") == ["plain"]
    assert len(expand_braces("{a,b}" * 12)) <= 4096  # bounded


@pytest.mark.parametrize(("pattern", "path", "match"), [
    ("*.py", "a.py", True), ("*.py", "src/deep/a.py", True), ("*.py", "a.pyc", False),
    ("*.{py,md}", "notes/readme.md", True), ("*.{py,md}", "x.txt", False),
    ("src/*.py", "src/a.py", True), ("src/*.py", "src/sub/a.py", False), ("src/**/*.py", "src/sub/deep/a.py", True),
    ("src/**/*.py", "src/a.py", True), ("**/test_*.py", "tests/unit/test_x.py", True), ("**/test_*.py", "test_x.py", True),
    ("file?.txt", "file1.txt", True), ("file?.txt", "file10.txt", False), ("[abc].txt", "b.txt", True),
    ("[!abc].txt", "b.txt", False), ("a.b", "axb", False), ("/src/*.py", "/src/a.py", True), ("*.PY", "a.py", False),
])
def test_glob_matching(pattern, path, match):
    assert glob_match(pattern, path) is match


def test_glob_ignore_case_and_windows_separators():
    assert glob_match("*.PY", "a.py", ignore_case=True)
    assert glob_match("src/*.py", "src\\a.py")
    assert compile_glob("[").fullmatch("[")  # an unclosed bracket is a literal


S = Stat(is_dir=False, size=100, mtime=1.0)


def test_a_partial_read_does_not_unlock_an_overwrite():
    guard = OverwriteGuard()
    assert "has not been read in full" in (guard.refusal("/f", S, "f") or "")
    guard.note_read("/f", S, first_line=1, last_line=3, total_lines=10)
    assert guard.refusal("/f", S) is not None  # BUG-6: three lines of ten unlock nothing
    guard.note_read("/f", S, first_line=7, last_line=10, total_lines=10)
    assert guard.refusal("/f", S) is not None  # still a gap (4-6)
    guard.note_read("/f", S, first_line=4, last_line=6, total_lines=10)
    assert guard.refusal("/f", S) is None


def test_overlapping_and_out_of_order_reads_merge():
    guard = OverwriteGuard()
    for lo, hi in ((5, 8), (1, 6), (9, 9), (7, 10)):
        guard.note_read("/f", S, first_line=lo, last_line=hi, total_lines=10)
    assert guard.fully_read("/f", S)


def test_a_changed_file_must_be_read_again():
    guard = OverwriteGuard()
    guard.note_read("/f", S, first_line=1, last_line=2, total_lines=2)
    assert guard.fully_read("/f", S)
    assert not guard.fully_read("/f", Stat(is_dir=False, size=101, mtime=1.0))
    assert not guard.fully_read("/f", Stat(is_dir=False, size=100, mtime=2.0))


def test_empty_and_new_files_and_files_this_session_created():
    guard = OverwriteGuard()
    assert guard.refusal("/new", None) is None  # nothing to overwrite
    empty = Stat(is_dir=False, size=0, mtime=1.0)
    guard.note_read("/empty", empty, first_line=0, last_line=0, total_lines=0)
    assert guard.refusal("/empty", empty) is None
    guard.note_created("/mine")
    guard.note_written("/mine", S, 4)
    assert guard.refusal("/mine", S) is None
    assert guard.refusal("/mine", Stat(is_dir=False, size=999, mtime=5.0)) is not None  # someone else changed it
    assert "folder" in (guard.refusal("/d", Stat(is_dir=True)) or "")


def test_whole_file_read_flag_and_case_insensitive_keys():
    guard = OverwriteGuard(case_insensitive=True)
    guard.note_read("/Dir/File.TXT", S, first_line=1, last_line=1, total_lines=50, whole_file=True)
    assert guard.fully_read("/dir/file.txt", S)
    guard.forget("/DIR/file.txt")
    assert not guard.fully_read("/dir/file.txt", S)
