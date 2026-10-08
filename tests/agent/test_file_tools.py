from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from custom_console.agent.permissions import UserPermissionDenied
from custom_console.agent.tools.filesystem import (
    apply_edit,
    filesystem_tools,
    glob_regex,
    grep_file,
    load_text,
    python_outline,
    save_text,
    TextFile,
)


def by_name(tools):
    return {tool.__name__: tool for tool in tools}


@pytest.fixture
def fs(ctx, files_dir):
    (files_dir / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    (files_dir / "sub").mkdir()
    (files_dir / "sub" / "deep.py").write_text(
        '"""Module."""\nimport os\n\nclass Box(Base):\n    """A box."""\n    def open(self, lid):\n        pass\n\n'
        "async def fetch(url, *rest, flag=1, **kw):\n    pass\n"
    )
    return by_name(filesystem_tools(ctx))


# --------------------------------------------------------------------------- #
# Existing exploration tools
# --------------------------------------------------------------------------- #


class TestExploration:
    def test_list(self, fs):
        assert fs["file_system_list"]().data == ["a.txt", "sub/"]
        assert fs["file_system_list"](path="sub").data == ["deep.py"]

    def test_list_errors_are_results_not_exceptions(self, fs):
        result = fs["file_system_list"](path="nope")
        assert not result.success and isinstance(result.error, FileNotFoundError)

    def test_read_full(self, fs):
        assert fs["file_system_read"]("a.txt").data == "one\ntwo\nthree\nfour\n"

    def test_read_range_is_one_based_inclusive_and_keeps_newlines(self, fs):
        assert fs["file_system_read"]("a.txt", mode="range", start_line=2, end_line=3).data == "two\nthree"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"mode": "range"},
            {"mode": "range", "start_line": 0, "end_line": 2},
            {"mode": "range", "start_line": 3, "end_line": 2},
            {"mode": "bogus"},
        ],
    )
    def test_read_invalid_arguments(self, fs, kwargs):
        result = fs["file_system_read"]("a.txt", **kwargs)
        assert not result.success and isinstance(result.error, ValueError)

    def test_read_summary_of_a_python_file(self, fs):
        outline = fs["file_system_read"]("sub/deep.py", mode="summary").data
        assert "class Box(Base):" in outline and "def open(self, lid)" in outline

    def test_read_summary_of_other_files_is_the_head(self, fs, files_dir):
        (files_dir / "long.txt").write_text("\n".join(f"l{i}" for i in range(200)))
        summary = fs["file_system_read"]("long.txt", mode="summary").data
        assert summary.splitlines()[0] == "l0" and len(summary.splitlines()) == 50

    def test_a_cut_read_says_which_lines_it_shows_and_where_to_go_on(self, fs):
        data = fs["file_system_read"]("a.txt", max_chars=5).data
        assert data.startswith("one\n[... cut at 5 characters: lines 1-1 of ")
        assert 'Read on with mode="range", start_line=2' in data

    def test_a_cut_range_counts_from_its_first_line(self, fs, files_dir):
        (files_dir / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 101)))
        data = fs["file_system_read"]("long.txt", mode="range", start_line=50, max_chars=30).data
        assert data.startswith("line 50\nline 51\nline 52\n[... cut") and "lines 50-52 of 100" in data
        assert "start_line=53" in data

    def test_line_numbers(self, fs, files_dir):
        (files_dir / "n.txt").write_text("a\nb\nc")
        assert fs["file_system_read"]("n.txt", mode="range", start_line=2, line_numbers=True).data == "     2\tb\n     3\tc"

    def test_reads_are_capped_by_the_context_window(self, fs, files_dir, ctx):
        ctx.window = lambda: 4_000  # at least 4,000 characters are always allowed
        (files_dir / "big.txt").write_text("x" * 100 + "\n" + "y" * 10_000)
        data = fs["file_system_read"]("big.txt", max_chars=1_000_000).data
        assert "cut at 4000 characters" in data

    def test_read_binary_file_fails_cleanly(self, fs, files_dir):
        (files_dir / "b.bin").write_bytes(b"\x00\x01")
        assert not fs["file_system_read"]("b.bin").success

    def test_stat(self, fs):
        assert set(fs["file_system_stat"]("a.txt").data) == {"readable", "writable", "executable"}

    def test_find(self, fs):
        assert fs["file_system_find"]("deep").data == ["sub/deep.py"]
        assert fs["file_system_find"]("deep", depth=1).data == []

    def test_cd_changes_where_relative_paths_resolve(self, fs):
        assert "Changed directory" in fs["file_system_cd"]("sub").data
        assert fs["file_system_list"]().data == ["deep.py"]

    def test_tree(self, fs):
        text = fs["file_system_tree"](depth=2).data
        assert "sub/" in text and "deep.py" in text

    def test_copy(self, fs, files_dir):
        assert fs["file_system_copy"]("a.txt", "b.txt").success
        assert (files_dir / "b.txt").read_text() == "one\ntwo\nthree\nfour\n"
        assert not fs["file_system_copy"]("sub", "sub2").success  # needs recursive
        assert fs["file_system_copy"]("sub", "sub2", recursive=True).success


def test_python_outline():
    source = "class A(B):\n    def f(self, x): ...\n    async def g(self): ...\n\ndef top(a, *args, k=1, **kw): ..."
    assert python_outline(source).splitlines() == [
        "class A(B):  [L1]",
        "    def f(self, x)  [L2]",
        "    async def g(self)  [L3]",
        "def top(a, *args, k, **kw)  [L5]",
    ]
    assert python_outline("x = 1") == "(no class or function)"
    assert "Syntax error" in python_outline("def (:")


# --------------------------------------------------------------------------- #
# glob and grep
# --------------------------------------------------------------------------- #


class TestGlob:
    @pytest.mark.parametrize(
        "pattern, path, expected",
        [
            ("*.py", "a.py", True),
            ("*.py", "src/a.py", True),  # no slash: any depth
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/deep/a.py", False),
            ("src/**/*.py", "src/deep/a.py", True),
            ("src/**/*.py", "src/a.py", True),
            ("**/test_*.py", "tests/unit/test_x.py", True),
            ("a?.txt", "ab.txt", True),
            ("a?.txt", "abc.txt", False),
            ("*.PY", "x.py", True),  # case-insensitive
            ("[ab].txt", "a.txt", True),
            ("[!ab].txt", "a.txt", False),
            ("a.b", "aXb", False),  # dots are literal
        ],
    )
    def test_glob_regex(self, pattern, path, expected):
        assert bool(glob_regex(pattern).match(path)) is expected

    def test_tool_returns_matching_files_newest_first_and_skips_noise(self, fs, files_dir):
        (files_dir / "sub" / "new.py").write_text("x")
        os.utime(files_dir / "sub" / "deep.py", (1_000_000, 1_000_000))
        (files_dir / ".git").mkdir()
        (files_dir / ".git" / "hook.py").write_text("x")
        (files_dir / "node_modules").mkdir()
        (files_dir / "node_modules" / "lib.py").write_text("x")

        result = fs["file_system_glob"]("**/*.py")
        assert result.data.splitlines() == ["sub/new.py", "sub/deep.py"]

    def test_directories_only_with_a_trailing_slash(self, fs):
        assert fs["file_system_glob"]("sub/").data == "sub/"
        assert fs["file_system_glob"]("*.nothing").data == "No match."

    def test_result_count_is_limited(self, fs, files_dir):
        for index in range(5):
            (files_dir / f"f{index}.log").write_text("x")
        data = fs["file_system_glob"]("*.log", max_results=2).data
        assert len(data.splitlines()) == 3 and "3 more" in data

    def test_path_must_be_a_directory(self, fs):
        assert isinstance(fs["file_system_glob"]("*", path="a.txt").error, NotADirectoryError)


class TestGrep:
    def test_grep_file_marks_context_lines(self):
        import re

        lines = ["a", "b", "match", "c", "d", "e", "match"]
        result = grep_file(re.compile("match"), lines, context=1)
        assert result == [
            (2, "b", False),
            (3, "match", True),
            (4, "c", False),
            (6, "e", False),
            (7, "match", True),
        ]

    def test_finds_lines_with_numbers(self, fs):
        data = fs["file_system_grep"]("two|four").data
        assert data.splitlines() == ["a.txt:2:two", "a.txt:4:four"]

    def test_include_filter_and_subfolders(self, fs):
        data = fs["file_system_grep"]("class", include="*.py").data
        assert data == "sub/deep.py:4:class Box(Base):"

    def test_single_file(self, fs):
        assert fs["file_system_grep"]("three", path="a.txt").data == "a.txt:3:three"

    def test_ignore_case_and_context(self, fs):
        data = fs["file_system_grep"]("TWO", path="a.txt", ignore_case=True, context=1).data
        assert data.splitlines() == ["a.txt-1-one", "a.txt:2:two", "a.txt-3-three"]

    def test_no_match(self, fs):
        assert fs["file_system_grep"]("zzz").data == "No match."

    def test_invalid_regex_is_a_clear_error(self, fs):
        result = fs["file_system_grep"]("(")
        assert not result.success and "Invalid regular expression" in str(result.error)

    def test_match_limit(self, fs, files_dir):
        (files_dir / "many.txt").write_text("hit\n" * 20)
        data = fs["file_system_grep"]("hit", path="many.txt", max_matches=3).data
        assert data.splitlines()[-1] == "[... stopped after 3 matches ...]" and data.count("hit") == 3

    def test_binary_files_are_skipped(self, fs, files_dir):
        (files_dir / "b.bin").write_bytes(b"hit\x00hit")
        assert fs["file_system_grep"]("hit").data == "No match."

    def test_long_lines_are_shortened(self, fs, files_dir):
        (files_dir / "wide.txt").write_text("hit " + "x" * 1000)
        assert len(fs["file_system_grep"]("hit", path="wide.txt").data) < 400


# --------------------------------------------------------------------------- #
# Text files: line endings, encoding
# --------------------------------------------------------------------------- #


class TestTextFiles:
    def test_crlf_and_bom_survive_a_round_trip(self, tmp_path):
        path = tmp_path / "f.txt"
        path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
        loaded = load_text(str(path))
        assert loaded.text == "one\ntwo\n" and loaded.newline == "\r\n" and loaded.bom
        save_text(str(path), TextFile(loaded.text.replace("two", "2"), loaded.newline, loaded.bom))
        assert path.read_bytes() == b"\xef\xbb\xbfone\r\n2\r\n"

    def test_non_utf8_and_binary_files_are_refused(self, tmp_path):
        (tmp_path / "latin.txt").write_bytes("é".encode("latin-1"))
        (tmp_path / "bin").write_bytes(b"a\x00b")
        with pytest.raises(ValueError, match="not valid UTF-8"):
            load_text(str(tmp_path / "latin.txt"))
        with pytest.raises(ValueError, match="binary"):
            load_text(str(tmp_path / "bin"))


class TestApplyEdit:
    def test_replaces_a_unique_occurrence(self):
        assert apply_edit("a b c", "b", "X") == ("a X c", 1)

    def test_ambiguous_text_needs_more_context_or_replace_all(self):
        with pytest.raises(ValueError, match="appears 2 times"):
            apply_edit("x x", "x", "y")
        assert apply_edit("x x", "x", "y", replace_all=True) == ("y y", 2)

    def test_missing_text_hints_about_whitespace(self):
        with pytest.raises(ValueError, match="not found"):
            apply_edit("abc", "zzz", "y")
        with pytest.raises(ValueError, match="indentation"):
            apply_edit("  abc", " abc\n", "y")

    @pytest.mark.parametrize("old, new", [("", "x"), ("a", "a")])
    def test_pointless_edits_are_refused(self, old, new):
        with pytest.raises(ValueError):
            apply_edit("abc", old, new)

    def test_windows_line_endings_in_the_arguments_are_normalised(self):
        assert apply_edit("a\nb\n", "a\r\nb", "X") == ("X\n", 1)


# --------------------------------------------------------------------------- #
# write / edit
# --------------------------------------------------------------------------- #


class TestWriteAndEdit:
    def test_write_creates_files_and_folders_and_reports_a_diff(self, fs, files_dir):
        result = fs["file_system_write"]("new/dir/n.txt", "hello\nworld\n")
        assert result.success and result.data["created"] is True and result.data["lines_added"] == 2
        assert (files_dir / "new" / "dir" / "n.txt").read_text() == "hello\nworld\n"
        assert result.diff == "+hello\n+world"

    def test_overwriting_requires_a_prior_read(self, fs, files_dir):
        refused = fs["file_system_write"]("a.txt", "new")
        assert isinstance(refused.error, PermissionError) and "not been read" in str(refused.error)
        assert (files_dir / "a.txt").read_text() == "one\ntwo\nthree\nfour\n"

        fs["file_system_read"]("a.txt")
        done = fs["file_system_write"]("a.txt", "one\n2\nthree\nfour\n")
        assert done.success and done.data == {"path": str(files_dir / "a.txt").replace("\\", "/"), "created": False, "lines_added": 1, "lines_removed": 1}
        assert "-two" in done.diff and "+2" in done.diff

    def test_overwrite_false_never_replaces(self, fs):
        fs["file_system_read"]("a.txt")
        assert isinstance(fs["file_system_write"]("a.txt", "x", overwrite=False).error, FileExistsError)

    def test_writing_a_directory_fails(self, fs):
        assert isinstance(fs["file_system_write"]("sub", "x").error, IsADirectoryError)

    def test_edit_replaces_text_and_keeps_crlf(self, fs, files_dir):
        (files_dir / "win.txt").write_bytes(b"alpha\r\nbeta\r\n")
        fs["file_system_read"]("win.txt")
        result = fs["file_system_edit"]("win.txt", "beta", "gamma")
        assert result.success and result.data["replacements"] == 1
        assert (files_dir / "win.txt").read_bytes() == b"alpha\r\ngamma\r\n"
        assert result.diff.splitlines()[-2:] == ["-beta", "+gamma"]

    def test_edit_requires_a_fresh_read(self, fs, files_dir):
        never_read = fs["file_system_edit"]("a.txt", "two", "2")
        assert isinstance(never_read.error, PermissionError)

        fs["file_system_read"]("a.txt")
        (files_dir / "a.txt").write_text("changed behind the agent's back\n")
        os.utime(files_dir / "a.txt", (2_000_000_000, 2_000_000_000))
        stale = fs["file_system_edit"]("a.txt", "changed", "x")
        assert "changed since it was read" in str(stale.error)

    def test_consecutive_edits_do_not_need_a_new_read(self, fs, files_dir):
        fs["file_system_read"]("a.txt")
        assert fs["file_system_edit"]("a.txt", "one", "1").success
        assert fs["file_system_edit"]("a.txt", "two", "2").success
        assert (files_dir / "a.txt").read_text() == "1\n2\nthree\nfour\n"

    def test_a_truncated_read_does_not_count_as_reading_the_file(self, fs):
        fs["file_system_read"]("a.txt", max_chars=3)
        assert isinstance(fs["file_system_edit"]("a.txt", "two", "2").error, PermissionError)

    def test_ambiguous_edit_fails_without_touching_the_file(self, fs, files_dir):
        (files_dir / "rep.txt").write_text("x\nx\n")
        fs["file_system_read"]("rep.txt")
        result = fs["file_system_edit"]("rep.txt", "x", "y")
        assert not result.success and (files_dir / "rep.txt").read_text() == "x\nx\n"
        assert fs["file_system_edit"]("rep.txt", "x", "y", replace_all=True).data["replacements"] == 2

    def test_edit_of_a_missing_file(self, fs):
        assert isinstance(fs["file_system_edit"]("ghost.txt", "a", "b").error, FileNotFoundError)

    def test_binary_file_cannot_be_edited(self, fs, files_dir):
        (files_dir / "b.bin").write_bytes(b"a\x00b")
        fs["file_system_read"]("b.bin")  # refused too, so it was never "read"
        assert not fs["file_system_edit"]("b.bin", "a", "c").success


# --------------------------------------------------------------------------- #
# move / remove
# --------------------------------------------------------------------------- #


class TestMoveAndRemove:
    def test_move_renames_and_moves_into_folders(self, fs, files_dir):
        assert fs["file_system_move"]("a.txt", "renamed.txt").success
        assert (files_dir / "renamed.txt").exists() and not (files_dir / "a.txt").exists()
        assert fs["file_system_move"]("renamed.txt", "sub").success
        assert (files_dir / "sub" / "renamed.txt").exists()

    def test_move_never_overwrites(self, fs, files_dir):
        (files_dir / "b.txt").write_text("b")
        result = fs["file_system_move"]("a.txt", "b.txt")
        assert isinstance(result.error, FileExistsError) and (files_dir / "a.txt").exists()

    def test_cannot_move_a_folder_into_itself(self, fs):
        assert "into itself" in str(fs["file_system_move"]("sub", "sub/inner").error)

    def test_remove_file_and_folder(self, fs, files_dir):
        assert fs["file_system_remove"]("a.txt").success and not (files_dir / "a.txt").exists()
        assert not fs["file_system_remove"]("sub").success  # not empty, recursive needed
        assert fs["file_system_remove"]("sub", recursive=True).success and not (files_dir / "sub").exists()

    def test_remove_refuses_the_folder_holding_the_working_directory(self, fs, files_dir):
        fs["file_system_cd"]("sub")
        result = fs["file_system_remove"](str(files_dir), recursive=True)
        assert not result.success and files_dir.exists()

    def test_remote_and_virtual_paths_are_rejected_for_local_only_operations(self, fs):
        assert not fs["file_system_move"]("/", "x").success
        assert not fs["file_system_write"]("reMarkable:/x.txt", "x").success


# --------------------------------------------------------------------------- #
# The free zone
# --------------------------------------------------------------------------- #


@pytest.fixture
def zoned(make_ctx, files_dir, tmp_path):
    """Level 0 (the strictest): everything outside the zone asks."""
    ctx, log = make_ctx(zone=True, auto_level=0, answer=False)
    (files_dir / "a.txt").write_text("one\ntwo\n")
    (files_dir / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("out\n")
    return by_name(filesystem_tools(ctx)), log, outside, ctx


class TestFreeZone:
    def test_reads_and_writes_inside_the_zone_never_ask(self, zoned, files_dir):
        tools, log, _, _ = zoned
        assert tools["file_system_list"]().success
        assert tools["file_system_read"]("a.txt").success
        assert tools["file_system_grep"]("one").success
        assert tools["file_system_write"]("sub/new.txt", "x").success
        assert tools["file_system_edit"]("a.txt", "one", "1").success
        assert tools["file_system_copy"]("a.txt", "sub/copy.txt").success
        assert tools["file_system_move"]("sub/copy.txt", "sub/moved.txt").success
        assert tools["file_system_remove"]("sub/moved.txt").success
        assert log.asked == []
        assert (files_dir / "a.txt").read_text() == "1\ntwo\n"

    def test_reading_outside_asks_like_before(self, zoned):
        tools, log, outside, _ = zoned
        result = tools["file_system_read"](str(outside / "o.txt"))
        assert isinstance(result.error, UserPermissionDenied)
        assert len(log.asked) == 1 and "file system read" in log.asked[0]

    def test_reading_outside_is_auto_accepted_at_level_1(self, make_ctx, tmp_path):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        (tmp_path / "o.txt").write_text("out\n")
        tools = by_name(filesystem_tools(ctx))
        assert tools["file_system_read"](str(tmp_path / "o.txt")).data == "out\n"
        assert log.asked == []  # level 1 auto-accepts reads

    @pytest.mark.parametrize(
        "tool, arguments",
        [
            ("file_system_write", {"path": "{o}/n.txt", "content": "x"}),
            ("file_system_remove", {"path": "{o}/o.txt"}),
            ("file_system_move", {"src": "{o}/o.txt", "dst": "{o}/p.txt"}),
            ("file_system_copy", {"src": "a.txt", "dst": "{o}/copy.txt"}),  # out of the zone
            ("file_system_copy", {"src": "{o}/o.txt", "dst": "in.txt"}),  # into the zone, from outside
        ],
    )
    def test_changes_involving_outside_paths_ask_and_can_be_refused(self, zoned, tool, arguments, files_dir):
        tools, log, outside, _ = zoned
        arguments = {key: value.format(o=str(outside).replace("\\", "/")) for key, value in arguments.items()}
        result = tools[tool](**arguments)
        assert isinstance(result.error, UserPermissionDenied) and len(log.asked) == 1
        assert sorted(p.name for p in outside.iterdir()) == ["o.txt"]  # untouched
        assert not (files_dir / "in.txt").exists()

    def test_the_zone_folder_itself_cannot_be_removed_or_moved_for_free(self, zoned, files_dir):
        tools, log, _, _ = zoned
        tools["file_system_cd"]("sub")
        assert isinstance(tools["file_system_remove"](str(files_dir), recursive=True).error, UserPermissionDenied)
        assert isinstance(tools["file_system_move"](str(files_dir), str(files_dir) + "_x").error, UserPermissionDenied)
        assert len(log.asked) == 2

    def test_dot_dot_cannot_escape_the_zone(self, zoned, files_dir):
        tools, log, outside, _ = zoned
        assert isinstance(tools["file_system_read"]("../outside/o.txt").error, UserPermissionDenied)
        assert isinstance(tools["file_system_write"]("sub/../../outside/x.txt", "x").error, UserPermissionDenied)
        assert not (outside / "x.txt").exists()

    def test_a_folder_with_the_same_prefix_is_outside(self, zoned, tmp_path):
        tools, log, _, _ = zoned
        sibling = tmp_path / "files_evil"
        sibling.mkdir()
        (sibling / "s.txt").write_text("s")
        assert isinstance(tools["file_system_read"](str(sibling / "s.txt")).error, UserPermissionDenied)

    def test_symlinks_leaving_the_zone_do_not_widen_it(self, zoned, files_dir):
        tools, log, outside, _ = zoned
        try:
            (files_dir / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted")
        assert isinstance(tools["file_system_read"]("link/o.txt").error, UserPermissionDenied)

    def test_no_zone_means_everything_keeps_its_normal_level(self, make_ctx, files_dir):
        ctx, log = make_ctx(zone=False, auto_level=1, answer=False)
        tools = by_name(filesystem_tools(ctx))
        assert tools["file_system_list"]().success and log.asked == []
        assert isinstance(tools["file_system_write"]("x.txt", "x").error, UserPermissionDenied)

    def test_the_question_for_an_edit_outside_the_zone_shows_the_diff(self, make_ctx, tmp_path):
        ctx, log = make_ctx(zone=False, auto_level=1, answer=False)
        target = tmp_path / "files" / "code.py"
        target.write_text("x = 1\ny = 2\n")
        tools = by_name(filesystem_tools(ctx))
        tools["file_system_read"](str(target))
        tools["file_system_edit"](str(target), "y = 2", "y = 3")
        assert "Agent wants to edit" in log.asked[0]
        assert "-y = 2" in log.asked[0] and "+y = 3" in log.asked[0]
        assert target.read_text() == "x = 1\ny = 2\n"  # refused

    def test_the_question_for_a_new_file_shows_its_content(self, make_ctx):
        ctx, log = make_ctx(zone=False, auto_level=1, answer=False)
        by_name(filesystem_tools(ctx))["file_system_write"]("n.txt", "first\nsecond\n")
        assert "Agent wants to create n.txt" in log.asked[0] and "+first" in log.asked[0]


# --------------------------------------------------------------------------- #
# Undo support
# --------------------------------------------------------------------------- #


class TestChangesCanBeUndone:
    def test_every_kind_of_change_is_recorded_and_reverted(self, ctx, files_dir):
        tools = by_name(filesystem_tools(ctx))
        (files_dir / "a.txt").write_text("original\n")
        (files_dir / "d").mkdir()
        (files_dir / "d" / "in.txt").write_text("inner")
        (files_dir / "m.txt").write_text("moveme")

        ctx.checkpoints.begin_turn("turn")
        tools["file_system_read"]("a.txt")
        tools["file_system_edit"]("a.txt", "original", "edited")
        tools["file_system_write"]("created.txt", "new")
        tools["file_system_remove"]("d", recursive=True)
        tools["file_system_move"]("m.txt", "moved.txt")
        tools["file_system_copy"]("a.txt", "copy.txt")
        assert not (files_dir / "d").exists()

        report = ctx.checkpoints.undo()
        assert len(report) == 5
        assert (files_dir / "a.txt").read_text() == "original\n"
        assert not (files_dir / "created.txt").exists() and not (files_dir / "copy.txt").exists()
        assert (files_dir / "d" / "in.txt").read_text() == "inner"
        assert (files_dir / "m.txt").read_text() == "moveme" and not (files_dir / "moved.txt").exists()


# --------------------------------------------------------------------------- #
# Edits that help the model, git-aware searches, protected paths
# --------------------------------------------------------------------------- #


class TestEditHelp:
    def test_trailing_spaces_need_not_match(self):
        text = "def f():   \n    return 1  \n"
        edited, count = apply_edit(text, "def f():\n    return 1", "def f():\n    return 2")
        assert (edited, count) == ("def f():\n    return 2\n", 1)

    def test_a_missing_text_shows_the_closest_passage_with_its_lines(self):
        text = "a = 1\ndef total(items):\n    return sum(items)\nb = 2\n"
        with pytest.raises(ValueError) as caught:
            apply_edit(text, "def total(item):\n    return sum(item)", "x")
        message = str(caught.value)
        assert "closest passage (lines 2-3" in message
        assert "     2\tdef total(items):\n     3\t    return sum(items)" in message

    def test_nothing_alike_shows_nothing(self):
        with pytest.raises(ValueError) as caught:
            apply_edit("alpha\nbeta\n", "completely different", "x")
        assert "closest" not in str(caught.value)


class TestMultiEdit:
    def test_several_replacements_at_once(self, fs, files_dir):
        fs["file_system_read"]("a.txt")
        result = fs["file_system_multi_edit"](
            "a.txt", [{"old_text": "one", "new_text": "1"}, {"old_text": "three", "new_text": "3"}]
        )
        assert result.success and result.data["replacements"] == 2
        assert (files_dir / "a.txt").read_text() == "1\ntwo\n3\nfour\n"

    def test_all_or_nothing(self, fs, files_dir):
        fs["file_system_read"]("a.txt")
        result = fs["file_system_multi_edit"](
            "a.txt", [{"old_text": "one", "new_text": "1"}, {"old_text": "nine", "new_text": "9"}]
        )
        assert not result.success and "Edit 2 of 2" in str(result.error) and "Nothing was changed" in str(result.error)
        assert (files_dir / "a.txt").read_text() == "one\ntwo\nthree\nfour\n"

    def test_edits_are_given_as_text_by_some_models(self, fs, files_dir):
        fs["file_system_read"]("a.txt")
        assert fs["file_system_multi_edit"]("a.txt", '[{"old_text": "two", "new_text": "2"}]').success
        assert (files_dir / "a.txt").read_text() == "one\n2\nthree\nfour\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
class TestGitAwareSearch:
    def repo(self, files_dir):
        subprocess.run(["git", "init", "-q", str(files_dir)], check=True)
        (files_dir / ".gitignore").write_text("build/\n*.log\n")
        (files_dir / "build").mkdir()
        (files_dir / "build" / "out.py").write_text("needle = 1\n")
        (files_dir / "debug.log").write_text("needle\n")
        (files_dir / "src").mkdir()
        (files_dir / "src" / "main.py").write_text("needle = 2\n")

    def test_grep_and_glob_leave_out_what_git_ignores(self, fs, files_dir):
        self.repo(files_dir)
        assert fs["file_system_grep"]("needle").data == "src/main.py:1:needle = 2"
        assert set(fs["file_system_glob"]("*.py").data.splitlines()) == {"src/main.py", "sub/deep.py"}
        assert fs["file_system_glob"]("src/").data == "src/"

    def test_outside_a_repository_everything_is_searched(self, fs, files_dir):
        (files_dir / "debug.log").write_text("needle\n")
        assert "debug.log:1:needle" in fs["file_system_grep"]("needle").data


class TestProtectedPaths:
    def test_changes_to_protected_paths_ask_even_in_the_zone(self, make_ctx, files_dir):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        tools = by_name(filesystem_tools(ctx))
        for path in (".git/hooks/pre-commit", ".vscode/tasks.json", ".env", "sub/.env.local", ".venv/x.py"):
            result = tools["file_system_write"](path, "x")
            assert isinstance(result.error, UserPermissionDenied), path
        assert len(log.asked) == 5

    def test_reading_them_stays_free_and_ordinary_files_too(self, make_ctx, files_dir):
        ctx, log = make_ctx(zone=True, auto_level=0, answer=False)
        (files_dir / ".env").write_text("A=1")
        tools = by_name(filesystem_tools(ctx))
        assert tools["file_system_read"](".env").success
        assert tools["file_system_write"]("notes.txt", "x").success
        assert log.asked == []

    def test_the_project_file_is_protected(self, files_dir):
        from custom_console.agent.zone import FreeZone

        zone = FreeZone(files_dir, protected_files=["AGENT.md"])
        assert not zone.contains(files_dir / "AGENT.md", write=True)
        assert zone.contains(files_dir / "AGENT.md") and zone.contains(files_dir / "sub" / "AGENT.md", write=True)


class TestSecretsAreAsked:
    def test_a_read_of_a_secret_outside_the_free_zone_asks_at_level_1(self, make_ctx):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        elsewhere = ctx.zone.root.parent / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("TOKEN=abc\n")
        (elsewhere / "notes.txt").write_text("hello\n")
        read = by_name(filesystem_tools(ctx))["file_system_read"]
        assert read(path=str(elsewhere / "notes.txt")).success and log.asked == []  # an ordinary read is free at level 1
        refused = read(path=str(elsewhere / ".env"))
        assert not refused.success and len(log.asked) == 1 and "abc" not in str(refused)

    def test_the_same_secret_inside_the_free_zone_stays_free(self, make_ctx):
        ctx, log = make_ctx(zone=True, auto_level=0, answer=False)
        (ctx.zone.root / ".env").write_text("A=1")
        assert by_name(filesystem_tools(ctx))["file_system_read"](path=str(ctx.zone.root / ".env")).success
        assert log.asked == []

    def test_a_search_through_a_folder_does_not_read_its_secrets(self, make_ctx):
        ctx, log = make_ctx(zone=True, auto_level=1)
        folder = ctx.zone.root
        (folder / ".env").write_text("TOKEN=needle\n")
        (folder / "a.txt").write_text("needle\n")
        found = by_name(filesystem_tools(ctx))["file_system_grep"](pattern="needle", path=str(folder))
        assert "a.txt" in str(found.data) and ".env" not in str(found.data)
