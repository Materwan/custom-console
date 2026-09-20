"""Tests unitaires de ``custom_agent.utils.file_utils``.

Ces tests couvrent les comportements locaux et les méthodes indépendantes
également utilisées par le backend reMarkable. Les appels à rmapi sont
remplacés par des mocks : les tests restent donc déterministes et ne
nécessitent ni tablette ni exécutable rmapi.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from custom_console.utils.file_utils import (
    FileManager,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    RemarkableBackend,
    RemarkableUnavailableError,
)


class TestRemarkableBackend:
    def test_resolve_relative_absolute_and_parent_paths(self):
        backend = RemarkableBackend("rmapi")
        backend.path = "/documents/work"

        assert backend._resolve(".") == "/documents/work"
        assert backend._resolve("notes") == "/documents/work/notes"
        assert backend._resolve("../archive") == "/documents/archive"
        assert backend._resolve("/root") == "/root"

    def test_parse_ls(self):
        output = """
[f] notes.pdf
[d] Projects
ignored output
[f] file with spaces.txt
"""

        assert RemarkableBackend._parse_ls(output) == [
            "notes.pdf",
            "Projects/",
            "file with spaces.txt",
        ]

    def test_parse_ls_typed(self):
        output = "[f] notes.pdf\n[d] Projects\n"

        assert RemarkableBackend._parse_ls_typed(output) == [
            ("notes.pdf", False),
            ("Projects", True),
        ]

    def test_listdir_runs_rmapi_and_parses_result(self):
        backend = RemarkableBackend("rmapi")
        backend._run = Mock(return_value="[f] a.pdf\n[d] Folder\n")

        assert backend.listdir(".") == ["a.pdf", "Folder/"]
        backend._run.assert_called_once_with("ls", "/")

    def test_listdir_raises_file_not_found_on_rmapi_error(self):
        backend = RemarkableBackend("rmapi")
        backend._run = Mock(return_value="ERROR: not found")

        with pytest.raises(FileNotFoundError, match=r"/missing"):
            backend.listdir("/missing")

    def test_cd_rejects_a_file(self):
        backend = RemarkableBackend("rmapi")
        backend._run = Mock(return_value="[f] report.pdf\n")

        with pytest.raises(NotADirectoryError):
            backend.cd("report.pdf")

        assert backend.pwd() == "/"

    def test_cd_updates_current_directory(self):
        backend = RemarkableBackend("rmapi")
        backend._run = Mock(return_value="[f] report.pdf\n[d] Archive\n")

        backend.cd("Documents")

        assert backend.pwd() == "/Documents"

    def test_get_and_put_detect_rmapi_errors(self):
        backend = RemarkableBackend("rmapi")
        backend._run = Mock(return_value="ERROR: failure")

        with pytest.raises(FileNotFoundError):
            backend.get("missing.pdf")
        with pytest.raises(FileNotFoundError):
            backend.put("local.pdf", "remote.pdf")


class TestFileManagerLocal:
    def test_init_starts_in_local_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        manager = FileManager()

        assert manager.mode == manager.MODE_LOCAL
        assert manager.in_root is False
        assert manager.in_remarkable is False
        assert manager.location == str(tmp_path).replace("\\", "/")

    def test_change_directory_and_go_home(self, tmp_path, monkeypatch):
        child = tmp_path / "child"
        child.mkdir()
        monkeypatch.chdir(tmp_path)
        manager = FileManager()

        manager.change_directory(str(child))
        assert Path.cwd() == child
        assert manager.local_location == str(child).replace("\\", "/")

        manager.change_directory("~")
        assert manager.mode == manager.MODE_LOCAL
        assert Path.cwd() == Path.home()

    def test_change_directory_raises_for_missing_path_and_file(self, tmp_path):
        manager = FileManager()
        file_path = tmp_path / "file.txt"
        file_path.write_text("content", encoding="utf-8")

        with pytest.raises(FileNotFoundError):
            manager.change_directory(str(tmp_path / "missing"))
        with pytest.raises(NotADirectoryError):
            manager.change_directory(str(file_path))

    def test_copy_file_and_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        source = tmp_path / "source.txt"
        source.write_text("hello", encoding="utf-8")
        destination = tmp_path / "destination.txt"
        manager = FileManager()

        manager.copy(str(source), str(destination), recursive=False)
        assert destination.read_text(encoding="utf-8") == "hello"

        directory = tmp_path / "directory"
        directory.mkdir()
        (directory / "nested.txt").write_text("nested", encoding="utf-8")
        copied_directory = tmp_path / "copied"

        with pytest.raises(ValueError, match="-r not specified"):
            manager.copy(str(directory), str(copied_directory), recursive=False)

        manager.copy(str(directory), str(copied_directory), recursive=True)
        assert (copied_directory / "nested.txt").read_text(encoding="utf-8") == "nested"

    def test_stat_returns_read_write_execute_permissions(self, tmp_path):
        file_path = tmp_path / "file.txt"
        file_path.write_text("content", encoding="utf-8")
        manager = FileManager()

        with patch(
            "custom_console.utils.file_utils.os.access", side_effect=[True, True, False]
        ):
            assert manager.stat(str(file_path)) == [True, True, False]

    def test_stat_rejects_missing_path(self, tmp_path):
        manager = FileManager()

        with pytest.raises(FileNotFoundError):
            manager.stat(str(tmp_path / "missing"))

    def test_find_matches_files_directories_and_depth(self, tmp_path):
        (tmp_path / "one.txt").write_text("1", encoding="utf-8")
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "two.txt").write_text("2", encoding="utf-8")
        manager = FileManager()

        result = manager.find(".*\\.txt", str(tmp_path), depth=2, strict=False)
        assert result == sorted([str(tmp_path / "one.txt"), str(folder / "two.txt")])

        directory_result = manager.find("folder/", str(tmp_path), depth=1, strict=True)
        assert directory_result == [str(folder)]

    def test_find_rejects_empty_pattern_and_invalid_root(self, tmp_path):
        manager = FileManager()

        with pytest.raises(ValueError, match="must not be empty"):
            manager.find("", str(tmp_path), depth=1, strict=False)
        with pytest.raises(NotADirectoryError):
            manager.find("file", str(tmp_path / "missing.txt"), depth=1, strict=False)

    def test_cat_reads_text_and_rejects_invalid_targets(self, tmp_path):
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello\nworld", encoding="utf-8")
        directory = tmp_path / "directory"
        directory.mkdir()
        manager = FileManager()

        assert manager.cat(str(file_path)) == "hello\nworld"
        with pytest.raises(NotAFileError):
            manager.cat(str(directory))
        with pytest.raises(FileNotFoundError):
            manager.cat(str(tmp_path / "missing.txt"))

    def test_list_hides_hidden_entries_by_default(self, tmp_path):
        (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
        (tmp_path / ".hidden.txt").write_text("hidden", encoding="utf-8")
        folder = tmp_path / "folder"
        folder.mkdir()
        manager = FileManager()

        assert manager.list(str(tmp_path), all=False) == ["folder/", "visible.txt"]
        assert sorted(manager.list(str(tmp_path), all=True)) == [
            ".hidden.txt",
            "folder/",
            "visible.txt",
        ]

    def test_root_lists_virtual_entries_and_rejects_file_operations(self):
        manager = FileManager()
        manager.mode = manager.MODE_ROOT
        manager._virtual_root_entries = Mock(return_value=["C:/", "reMarkable/"])

        assert manager.list(".", all=False) == ["C:/", "reMarkable/"]
        with pytest.raises(IsVirtualRootError):
            manager.cat("file.txt")
        with pytest.raises(IsVirtualRootError):
            manager.stat("file.txt")
        with pytest.raises(IsVirtualRootError):
            manager.copy("a", "b", recursive=False)

    def test_in_remarkable_property_keeps_compatibility(self):
        manager = FileManager()

        manager.in_remarkable = True
        assert manager.mode == manager.MODE_REMARKABLE
        assert manager.in_remarkable is True

        manager.in_remarkable = False
        assert manager.mode == manager.MODE_LOCAL
        assert manager.in_remarkable is False


class TestFileManagerRemarkable:
    def test_change_directory_to_remarkable_requires_configuration(self):
        manager = FileManager()
        manager.remarkable = None

        with pytest.raises(RemarkableUnavailableError):
            manager.change_directory("remarkable:/Documents")

    def test_change_directory_to_remarkable_resets_remote_path(self):
        manager = FileManager()
        backend = Mock()
        backend.path = "/old/path"
        manager.remarkable = backend

        manager.change_directory("remarkable:/Documents")

        assert manager.mode == manager.MODE_REMARKABLE
        assert backend.path == "/"
        backend.cd.assert_called_once_with("Documents")

    def test_cat_is_not_supported_on_remarkable(self):
        manager = FileManager()
        manager.mode = manager.MODE_REMARKABLE
        manager.remarkable = Mock()

        with pytest.raises(InReMarkableError):
            manager.cat("notes.pdf")

    def test_list_and_stat_use_remarkable_backend(self):
        manager = FileManager()
        manager.mode = manager.MODE_REMARKABLE
        backend = Mock()
        backend.listdir.return_value = ["visible.pdf", ".hidden.pdf"]
        backend.stat_entry.return_value = "visible.pdf"
        manager.remarkable = backend

        assert manager.list(".", all=False) == ["visible.pdf"]
        assert manager.list(".", all=True) == ["visible.pdf", ".hidden.pdf"]
        assert manager.stat("visible.pdf") == [True, True, True]

    def test_copy_local_file_to_remarkable(self, tmp_path):
        source = tmp_path / "source.txt"
        source.write_text("data", encoding="utf-8")
        manager = FileManager()
        backend = Mock()
        manager.remarkable = backend

        manager.copy(str(source), "remarkable:/destination.txt", recursive=False)

        backend.put.assert_called_once_with(
            str(source).replace("\\", "/"), "/destination.txt"
        )
