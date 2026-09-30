"""Tests for FileManager (local tree on tmp_path, reMarkable backend mocked)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_console.fs import (
    BinaryFileError,
    FileManager,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    Permissions,
    RemarkableUnavailableError,
    UnsafeOperationError,
)


@pytest.fixture
def project_tree(tmp_path):
    """
    tmp_path/
        report.txt, summary.md, .hidden.txt
        subdir/ report_old.txt, .hidden2, nested/data.csv
    """
    (tmp_path / "report.txt").write_text("root report")
    (tmp_path / "summary.md").write_text("root summary")
    (tmp_path / ".hidden.txt").write_text("hidden")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "report_old.txt").write_text("old report")
    (subdir / ".hidden2").write_text("hidden2")
    (subdir / "nested").mkdir()
    (subdir / "nested" / "data.csv").write_text("a,b,c")
    return tmp_path


@pytest.fixture
def fm(project_tree):
    return FileManager(start_dir=str(project_tree))


@pytest.fixture
def remote_fm(tmp_path, project_tree):
    exe = tmp_path / "rmapi.exe"
    exe.write_text("not a real binary")
    manager = FileManager(rmapi_path=exe, start_dir=str(project_tree))
    assert manager.remarkable is not None
    manager.remarkable.MIN_CALL_INTERVAL = 0.0
    return manager


def posix(path) -> str:
    return str(path).replace("\\", "/")


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


class TestState:
    def test_starts_local(self, fm, project_tree):
        assert fm.mode == FileManager.MODE_LOCAL
        assert not fm.in_remarkable and not fm.in_root
        assert fm.location == posix(project_tree)

    def test_no_remarkable_without_rmapi(self, fm):
        assert fm.remarkable is None

    def test_does_not_touch_process_cwd(self, fm):
        before = os.getcwd()
        fm.change_directory("subdir")
        assert os.getcwd() == before

    def test_clone_is_independent(self, fm):
        other = fm.clone()
        other.change_directory("subdir")
        assert other.local_location != fm.local_location
        assert other.local_location.endswith("/subdir")


class TestChangeDirectoryLocal:
    def test_into_subdir_and_back(self, fm, project_tree):
        fm.change_directory("subdir/nested")
        assert fm.location == posix(project_tree / "subdir" / "nested")
        fm.change_directory("../..")
        assert fm.location == posix(project_tree)

    def test_absolute_path(self, fm, project_tree):
        fm.change_directory(str(project_tree / "subdir"))
        assert fm.location == posix(project_tree / "subdir")

    def test_missing(self, fm):
        with pytest.raises(FileNotFoundError):
            fm.change_directory("does_not_exist")

    def test_file(self, fm):
        with pytest.raises(NotADirectoryError):
            fm.change_directory("report.txt")

    def test_home(self, fm):
        fm.change_directory("~")
        assert fm.mode == FileManager.MODE_LOCAL
        assert fm.location == posix(Path.home())

    def test_remote_without_remarkable(self, fm):
        with pytest.raises(RemarkableUnavailableError):
            fm.change_directory("reMarkable:/Notes")
        with pytest.raises(RemarkableUnavailableError):
            fm.change_directory("/reMarkable")


class TestVirtualRoot:
    def test_slash_switches_to_root(self, fm):
        fm.change_directory("/")
        assert fm.mode == FileManager.MODE_ROOT and fm.location == "/"

    def test_listdir_shows_virtual_entries(self, fm):
        fm.change_directory("/")
        entries = fm.listdir(".")
        assert "reMarkable/" in entries and "wsl-Ubuntu/" in entries

    def test_unknown_relative_path_not_found(self, fm):
        fm.change_directory("/")
        with pytest.raises(FileNotFoundError):
            fm.listdir("unknown")

    def test_file_operations_refused(self, fm):
        fm.change_directory("/")
        with pytest.raises(IsVirtualRootError):
            fm.cat("anything")
        with pytest.raises(IsVirtualRootError):
            fm.stat("anything")
        with pytest.raises(IsVirtualRootError):
            fm.find("x", ".", 3)
        with pytest.raises(IsVirtualRootError):
            fm.copy("a", "b")
        with pytest.raises(IsVirtualRootError):
            fm.remove("a")
        with pytest.raises(IsVirtualRootError):
            fm.tree(".")

    def test_absolute_virtual_path_works_from_anywhere(self, fm):
        drive = Path.home().drive
        fm.change_directory(f"/{drive}/")
        assert fm.mode == FileManager.MODE_LOCAL
        assert fm.location.upper().startswith(drive.upper())

    def test_drive_from_root_by_relative_name(self, fm):
        drive = Path.home().drive
        fm.change_directory("/")
        fm.change_directory(f"{drive}/")
        assert fm.mode == FileManager.MODE_LOCAL

    def test_unknown_drive(self, fm):
        with pytest.raises(FileNotFoundError):
            fm.change_directory("/Q:/some/path")

    def test_wsl_entry_routes_to_local_filesystem(self, fm):
        # Fails cleanly (or succeeds) depending on the machine: it must never
        # be treated as a reMarkable path.
        try:
            fm.change_directory("/wsl-Ubuntu/home")
        except FileNotFoundError:
            pass
        assert fm.mode != FileManager.MODE_REMARKABLE


# --------------------------------------------------------------------------- #
# Listing / tree / find
# --------------------------------------------------------------------------- #


class TestListdir:
    def test_hides_dotfiles_by_default(self, fm):
        entries = fm.listdir(".")
        assert "report.txt" in entries and ".hidden.txt" not in entries

    def test_show_hidden(self, fm):
        assert ".hidden.txt" in fm.listdir(".", show_hidden=True)

    def test_directories_have_trailing_slash_and_names_are_not_quoted(self, fm, project_tree):
        (project_tree / "a file.txt").write_text("x")
        entries = fm.listdir(".")
        assert "subdir/" in entries
        assert "a file.txt" in entries

    def test_sorted_case_insensitively(self, fm, project_tree):
        (project_tree / "Alpha.txt").write_text("x")
        entries = fm.listdir(".")
        assert entries == sorted(entries, key=str.lower)

    def test_file_and_missing(self, fm):
        with pytest.raises(NotADirectoryError):
            fm.listdir("report.txt")
        with pytest.raises(FileNotFoundError):
            fm.listdir("nope")


class TestTree:
    def test_layout(self, fm, project_tree):
        lines = fm.tree(".", depth=5)
        assert lines[0] == f"{project_tree.name}/"
        assert "    report.txt" in lines
        assert "    subdir/" in lines
        assert "        report_old.txt" in lines
        assert "        nested/" in lines
        assert "            data.csv" in lines
        assert not any(".hidden" in line for line in lines)

    def test_files_precede_directories(self, fm):
        lines = fm.tree(".", depth=1)
        assert lines.index("    summary.md") < lines.index("    subdir/")

    def test_depth_limit_shows_ellipsis(self, fm):
        lines = fm.tree(".", depth=1)
        assert "        ..." in lines
        assert not any("report_old" in line for line in lines)

    def test_unlimited_depth(self, fm):
        assert any("data.csv" in line for line in fm.tree(".", depth=-1))

    def test_show_hidden(self, fm):
        assert any(".hidden2" in line for line in fm.tree(".", depth=5, show_hidden=True))

    def test_missing_path_raises(self, fm):
        with pytest.raises(FileNotFoundError):
            fm.tree("nope")


class TestFind:
    def test_substring(self, fm):
        names = {os.path.basename(p) for p in fm.find("report", ".", 5)}
        assert names == {"report.txt", "report_old.txt"}

    def test_results_are_relative_to_typed_path(self, fm):
        assert "subdir/report_old.txt" in fm.find("report_old", ".", 5)
        assert any(p.endswith("subdir/nested/data.csv") for p in fm.find("data", "subdir", 5))

    def test_strict_requires_whole_name(self, fm):
        assert fm.find("report", ".", 5, strict=True) == []
        assert fm.find(re.escape("report.txt"), ".", 5, strict=True) == ["report.txt"]

    def test_depth(self, fm):
        assert fm.find("data", ".", 1) == []
        assert fm.find("data", ".", 5)

    def test_dir_only(self, fm):
        assert fm.find("sub/", ".", 5) == ["subdir"]

    def test_invalid_regex_falls_back_to_literal(self, fm, project_tree):
        (project_tree / "a(b.txt").write_text("x")
        assert fm.find("a(b", ".", 1) == ["a(b.txt"]

    def test_errors(self, fm, project_tree):
        with pytest.raises(ValueError):
            fm.find("", ".", 5)
        with pytest.raises(NotADirectoryError):
            fm.find("x", "report.txt", 5)
        with pytest.raises(FileNotFoundError):
            fm.find("x", "nope", 5)


# --------------------------------------------------------------------------- #
# read / stat
# --------------------------------------------------------------------------- #


class TestRead:
    def test_cat(self, fm):
        assert fm.cat("report.txt") == "root report"

    def test_read_truncates(self, fm):
        result = fm.read("report.txt", max_bytes=4)
        assert result.text == "root" and result.truncated is True
        assert fm.read("report.txt", max_bytes=100).truncated is False

    def test_directory_and_missing(self, fm):
        with pytest.raises(NotAFileError):
            fm.cat("subdir")
        with pytest.raises(FileNotFoundError):
            fm.cat("nope.txt")

    def test_binary_file(self, fm, project_tree):
        (project_tree / "blob.bin").write_bytes(b"\x00\x01\x02")
        with pytest.raises(BinaryFileError):
            fm.cat("blob.bin")

    def test_invalid_utf8_is_replaced_not_fatal(self, fm, project_tree):
        (project_tree / "latin.txt").write_bytes("café".encode("latin-1"))
        assert fm.cat("latin.txt").startswith("caf")

    def test_remarkable_mode(self, fm):
        fm.mode = FileManager.MODE_REMARKABLE
        with pytest.raises(InReMarkableError):
            fm.cat("notebook.rm")


class TestStat:
    def test_flags(self, fm, project_tree):
        target = project_tree / "report.txt"
        assert fm.stat("report.txt") == Permissions(
            os.access(target, os.R_OK), os.access(target, os.W_OK), os.access(target, os.X_OK)
        )

    def test_directory_and_missing(self, fm):
        assert fm.stat("subdir").readable
        with pytest.raises(FileNotFoundError):
            fm.stat("nope.txt")


# --------------------------------------------------------------------------- #
# copy / remove (local)
# --------------------------------------------------------------------------- #


class TestCopyLocal:
    def test_file(self, fm, project_tree):
        seen = []
        assert fm.copy("report.txt", "report_copy.txt", on_file=seen.append) == 1
        assert (project_tree / "report_copy.txt").read_text() == "root report"
        assert len(seen) == 1

    def test_file_into_existing_directory(self, fm, project_tree):
        fm.copy("report.txt", "subdir")
        assert (project_tree / "subdir" / "report.txt").is_file()

    def test_directory_recursive_to_new_name(self, fm, project_tree):
        assert fm.copy("subdir", "subdir_copy", recursive=True) == 3
        assert (project_tree / "subdir_copy" / "report_old.txt").is_file()
        assert (project_tree / "subdir_copy" / "nested" / "data.csv").is_file()

    def test_directory_into_existing_directory_keeps_its_name(self, fm, project_tree):
        (project_tree / "dest").mkdir()
        fm.copy("subdir", "dest", recursive=True)
        assert (project_tree / "dest" / "subdir" / "nested" / "data.csv").is_file()

    def test_directory_requires_recursive(self, fm):
        with pytest.raises(ValueError, match="-r"):
            fm.copy("subdir", "subdir_copy")

    def test_directory_into_itself(self, fm):
        with pytest.raises(ValueError, match="itself"):
            fm.copy("subdir", "subdir/nested", recursive=True)

    def test_missing_source(self, fm):
        with pytest.raises(FileNotFoundError):
            fm.copy("nope.txt", "dst.txt")


class TestRemoveLocal:
    def test_file(self, fm, project_tree):
        fm.remove("report.txt")
        assert not (project_tree / "report.txt").exists()

    def test_empty_directory(self, fm, project_tree):
        (project_tree / "empty").mkdir()
        fm.remove("empty")
        assert not (project_tree / "empty").exists()

    def test_non_empty_directory_needs_recursive(self, fm, project_tree):
        with pytest.raises(OSError):
            fm.remove("subdir")
        assert (project_tree / "subdir").exists()
        fm.remove("subdir", recursive=True)
        assert not (project_tree / "subdir").exists()

    def test_missing(self, fm):
        with pytest.raises(FileNotFoundError):
            fm.remove("nope.txt")

    def test_refuses_current_directory_and_ancestors(self, fm, project_tree):
        fm.change_directory("subdir")
        with pytest.raises(UnsafeOperationError):
            fm.remove(".", recursive=True)
        with pytest.raises(UnsafeOperationError):
            fm.remove("..", recursive=True)
        assert (project_tree / "subdir").exists()

    def test_refuses_drive_root_and_home(self, fm):
        with pytest.raises(UnsafeOperationError):
            fm.remove(f"/{Path.home().drive}/", recursive=True)
        with pytest.raises(UnsafeOperationError):
            fm.remove("~", recursive=True)

    def test_symlink_is_unlinked_not_followed(self, fm, project_tree):
        link = project_tree / "link"
        try:
            link.symlink_to(project_tree / "subdir", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted")
        fm.remove("link", recursive=True)
        assert not link.exists() and (project_tree / "subdir" / "nested").exists()


# --------------------------------------------------------------------------- #
# Completion helper
# --------------------------------------------------------------------------- #


class TestSuggest:
    def test_current_directory(self, fm):
        assert ("report.txt", False) in fm.suggest("rep")
        assert ("subdir", True) in fm.suggest("SUB")

    def test_nested_directory_keeps_typed_prefix(self, fm):
        assert ("subdir/nested", True) in fm.suggest("subdir/ne")

    def test_backslashes_are_accepted(self, fm):
        assert ("subdir/nested", True) in fm.suggest("subdir\\ne")

    def test_root(self, fm):
        names = {text for text, _ in fm.suggest("/")}
        assert "/reMarkable" in names

    def test_unknown_directory(self, fm):
        assert fm.suggest("nope/x") == []


# --------------------------------------------------------------------------- #
# reMarkable (backend methods mocked)
# --------------------------------------------------------------------------- #


class TestRemarkableNavigation:
    def test_cd_via_prefix(self, remote_fm):
        remote_fm.remarkable.cd = MagicMock()
        remote_fm.change_directory("reMarkable:/Notes")
        assert remote_fm.mode == FileManager.MODE_REMARKABLE
        remote_fm.remarkable.cd.assert_called_once_with("/Notes")

    def test_cd_remote_root_does_not_call_rmapi(self, remote_fm):
        remote_fm.remarkable.cd = MagicMock()
        remote_fm.change_directory("/reMarkable")
        assert remote_fm.mode == FileManager.MODE_REMARKABLE
        assert remote_fm.location == "reMarkable:/"
        remote_fm.remarkable.cd.assert_not_called()

    def test_failed_cd_keeps_previous_mode(self, remote_fm):
        remote_fm.remarkable.cd = MagicMock(side_effect=FileNotFoundError("nope"))
        with pytest.raises(FileNotFoundError):
            remote_fm.change_directory("reMarkable:/Missing")
        assert remote_fm.mode == FileManager.MODE_LOCAL

    def test_cd_relative_inside_remarkable(self, remote_fm):
        remote_fm.remarkable.cd = MagicMock()
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.path = "/Notes"
        remote_fm.change_directory("Physics")
        remote_fm.remarkable.cd.assert_called_with("/Notes/Physics")

    def test_listdir(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.scandir = MagicMock(return_value=[("a.pdf", False), ("Notes", True)])
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        assert remote_fm.listdir(".") == ["a.pdf", "Notes/"]

    def test_tree_and_find_work_on_the_tablet(self, remote_fm):
        listings = {"/": [("Notes", True), ("a.pdf", False)], "/Notes": [("Physics.pdf", False)]}
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        remote_fm.remarkable.scandir = MagicMock(side_effect=lambda p=".": listings[remote_fm.remarkable.resolve(p)])
        assert remote_fm.tree(".", depth=3) == ["/", "    a.pdf", "    Notes/", "        Physics.pdf"]
        assert remote_fm.find("phys", ".", 5) == ["/Notes/Physics.pdf"]

    def test_stat(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(return_value=False)
        assert remote_fm.stat("a.pdf") == Permissions(True, True, True)

    def test_stat_missing(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(side_effect=FileNotFoundError("x"))
        with pytest.raises(FileNotFoundError):
            remote_fm.stat("missing.pdf")

    def test_clone_copies_remote_location(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.path = "/Notes"
        other = remote_fm.clone()
        assert other.location == "reMarkable:/Notes"
        other.remarkable.path = "/Other"
        assert remote_fm.remarkable.path == "/Notes"

    def test_remove_refuses_whole_tablet(self, remote_fm):
        with pytest.raises(UnsafeOperationError):
            remote_fm.remove("/reMarkable", recursive=True)


class TestRemarkableCopy:
    def test_local_file_to_remarkable(self, remote_fm, project_tree):
        remote_fm.remarkable.put = MagicMock()
        remote_fm.remarkable.is_dir = MagicMock(side_effect=FileNotFoundError("new name"))

        remote_fm.copy("report.txt", "reMarkable:/Notes/report.txt")

        args, _ = remote_fm.remarkable.put.call_args
        assert args == (posix(project_tree / "report.txt"), "/Notes")

    def test_local_file_into_existing_remote_folder(self, remote_fm, project_tree):
        remote_fm.remarkable.put = MagicMock()
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        remote_fm.copy("report.txt", "/reMarkable/Notes")
        assert remote_fm.remarkable.put.call_args.args[1] == "/Notes"

    def test_local_directory_upload_is_refused(self, remote_fm):
        with pytest.raises(ValueError):
            remote_fm.copy("subdir", "reMarkable:/Notes", recursive=True)

    def test_remarkable_file_to_local(self, remote_fm, tmp_path):
        remote_fm.remarkable.is_dir = MagicMock(return_value=False)
        remote_fm.remarkable.get = MagicMock()
        dest = tmp_path / "downloads"
        seen = []

        assert remote_fm.copy("reMarkable:/Notes/notes.pdf", str(dest), on_file=seen.append) == 1

        remote_fm.remarkable.get.assert_called_once_with("/Notes/notes.pdf", posix(dest))
        assert seen == ["/Notes/notes.pdf"]

    def test_remarkable_directory_requires_recursive(self, remote_fm, tmp_path):
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        with pytest.raises(ValueError, match="-r"):
            remote_fm.copy("reMarkable:/Notes", str(tmp_path / "x"))

    def test_remarkable_directory_recursive_goes_into_existing_dest(self, remote_fm, tmp_path):
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        remote_fm.remarkable.download_tree = MagicMock(return_value=5)
        existing = tmp_path / "existing"
        existing.mkdir()

        assert remote_fm.copy("reMarkable:/Notes", str(existing), recursive=True) == 5

        expected = posix(existing / "Notes")
        assert remote_fm.remarkable.download_tree.call_args.args[:2] == ("/Notes", expected)
        assert os.path.isdir(expected)

    def test_within_the_tablet_folder_unsupported(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(return_value=True)
        with pytest.raises(ValueError):
            remote_fm.copy("SourceDir", "DestDir", recursive=True)

    def test_within_the_tablet_file(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(return_value=False)

        def fake_get(remote, dest):
            with open(os.path.join(dest, "notes.pdf"), "w") as handle:
                handle.write("content")

        remote_fm.remarkable.get = MagicMock(side_effect=fake_get)
        remote_fm.remarkable.put = MagicMock()

        remote_fm.copy("notes.pdf", "Archive/notes.pdf")

        remote_fm.remarkable.get.assert_called_once()
        assert remote_fm.remarkable.put.call_args.args[1] == "/Archive"

    def test_within_the_tablet_missing_source(self, remote_fm):
        remote_fm.mode = FileManager.MODE_REMARKABLE
        remote_fm.remarkable.is_dir = MagicMock(return_value=False)
        remote_fm.remarkable.get = MagicMock()  # downloads nothing
        with pytest.raises(FileNotFoundError):
            remote_fm.copy("missing.pdf", "Archive/missing.pdf")

    def test_without_backend(self, fm):
        with pytest.raises(RemarkableUnavailableError):
            fm.copy("reMarkable:/a", "b")
