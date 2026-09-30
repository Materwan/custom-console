"""Tests complets pour `custom_console.utils.file_utils`.

Emplacement suggéré : tests/test_file_utils.py à la racine du projet
(à côté de `src/`), de façon à ce que `custom_console` soit importable
normalement (package installé en mode editable, ou `src` ajouté au
PYTHONPATH / rootdir de pytest via un `conftest.py` / `pyproject.toml`).

Organisation :
- `TestRemarkableBackend`      : unités pures sur `RemarkableBackend`,
  avec `subprocess.run` mocké (aucun vrai `rmapi.exe` n'est appelé).
- `TestFileManagerLocal*`      : `FileManager` en mode local uniquement
  (reMarkable désactivé), sur une arborescence temporaire (`tmp_path`).
- `TestFileManagerRoot`        : navigation sur la racine virtuelle "/".
- `TestFileManagerRemarkable*` : `FileManager` avec reMarkable "activé"
  (RMAPI_PATH pointe vers un fichier bidon), mais dont les méthodes de
  `RemarkableBackend` sont directement mockées (pas de subprocess réel).

Aucun test ne touche au réseau, au vrai `rmapi.exe`, ni au vrai
Explorateur Windows.
"""

from __future__ import annotations

import os
import re
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_console.utils import file_utils
from custom_console.utils.file_utils import (
    FileManager,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    RemarkableBackend,
    RemarkableUnavailableError,
    VIRTUAL_ROOT_STATIC_ENTRIES,
    WSL_UBUNTU_PATH,
)

# --------------------------------------------------------------------------- #
# Helpers / fixtures communs
# --------------------------------------------------------------------------- #


class FakeCompletedProcess:
    """Substitut minimal de `subprocess.CompletedProcess` pour les tests."""

    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


def make_fake_run(script):
    """Construit un faux `subprocess.run` piloté par une liste de réponses.

    `script` est une liste de chaînes (la sortie combinée stdout+stderr à
    renvoyer, dans l'ordre des appels successifs). Le dernier élément est
    réutilisé indéfiniment une fois la liste épuisée.
    """
    calls = []

    def fake_run(
        args, capture_output=True, encoding="utf-8", errors="replace", cwd=None
    ):
        calls.append({"args": list(args), "cwd": cwd})
        idx = min(len(calls) - 1, len(script) - 1)
        return FakeCompletedProcess(stdout=script[idx])

    fake_run.calls = calls
    return fake_run


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Neutralise tous les `time.sleep` du module pour des tests rapides."""
    monkeypatch.setattr(file_utils.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def backend(monkeypatch):
    """Un `RemarkableBackend` prêt à l'emploi, sans délai anti rate-limit."""
    b = RemarkableBackend(exe_path="fake_rmapi.exe")
    b.MIN_CALL_INTERVAL = 0.0
    return b


@pytest.fixture
def project_tree(tmp_path):
    """Petite arborescence locale réutilisée par plusieurs tests.

    tmp_path/
        report.txt
        summary.md
        .hidden.txt
        subdir/
            report_old.txt
            .hidden2
            nested/
                data.csv
    """
    (tmp_path / "report.txt").write_text("root report")
    (tmp_path / "summary.md").write_text("root summary")
    (tmp_path / ".hidden.txt").write_text("hidden")

    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "report_old.txt").write_text("old report")
    (subdir / ".hidden2").write_text("hidden2")

    nested = subdir / "nested"
    nested.mkdir()
    (nested / "data.csv").write_text("a,b,c")

    return tmp_path


@pytest.fixture
def local_file_manager(monkeypatch, tmp_path):
    """`FileManager` en mode purement local (reMarkable désactivé)."""
    monkeypatch.setattr(file_utils, "RMAPI_PATH", None)
    monkeypatch.chdir(tmp_path)
    return FileManager()


@pytest.fixture
def remarkable_capable_file_manager(monkeypatch, tmp_path):
    """`FileManager` avec reMarkable "disponible" (fichier bidon comme exe).

    Les appels réels à `rmapi.exe` ne sont jamais faits : chaque test doit
    mocker les méthodes de `fm.remarkable` dont il a besoin.
    """
    fake_exe = tmp_path / "rmapi.exe"
    fake_exe.write_text("not a real binary")
    monkeypatch.setattr(file_utils, "RMAPI_PATH", str(fake_exe))

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    monkeypatch.chdir(local_dir)

    fm = FileManager()
    assert fm.remarkable is not None
    fm.remarkable.MIN_CALL_INTERVAL = 0.0
    return fm


# --------------------------------------------------------------------------- #
# RemarkableBackend — unités pures (subprocess mocké)
# --------------------------------------------------------------------------- #


class TestRemarkableBackendParsing:
    def test_parse_ls_separates_files_and_dirs(self):
        output = "[f]     notes.txt\n[d]     Documents\n[f]     trailing space.pdf \n"
        entries = RemarkableBackend._parse_ls(output)
        assert entries == ["notes.txt", "Documents/", "trailing space.pdf "]

    def test_parse_ls_ignores_blank_lines(self):
        output = "\n[f]     a.txt\n\n   \n[d]     b\n"
        assert RemarkableBackend._parse_ls(output) == ["a.txt", "b/"]

    def test_parse_ls_typed(self):
        output = "[f]     a.txt\n[d]     b\n"
        assert RemarkableBackend._parse_ls_typed(output) == [
            ("a.txt", False),
            ("b", True),
        ]

    def test_parse_ls_ignores_error_lines(self):
        output = "ERROR: something went wrong\n"
        assert RemarkableBackend._parse_ls(output) == []


class TestRemarkableBackendResolve:
    def test_resolve_relative(self, backend):
        backend.path = "/Notes"
        assert backend._resolve("sub") == "/Notes/sub"

    def test_resolve_dot(self, backend):
        backend.path = "/Notes"
        assert backend._resolve(".") == "/Notes"
        assert backend._resolve("") == "/Notes"

    def test_resolve_absolute(self, backend):
        backend.path = "/Notes"
        assert backend._resolve("/Other") == "/Other"

    def test_resolve_parent(self, backend):
        backend.path = "/Notes/sub"
        assert backend._resolve("..") == "/Notes"

    def test_resolve_root_normalizes_to_slash(self, backend):
        backend.path = "/"
        assert backend._resolve(".") == "/"


class TestRemarkableBackendListdir:
    def test_listdir_success(self, backend, monkeypatch):
        fake_run = make_fake_run(["[f]     a.pdf\n[d]     folder\n"])
        monkeypatch.setattr(file_utils.subprocess, "run", fake_run)

        result = backend.listdir(".")

        assert result == ["a.pdf", "folder/"]
        assert fake_run.calls[0]["args"][1:] == ["ls", "/"]

    def test_listdir_error_raises_filenotfound(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["ERROR: no such node\n"])
        )
        with pytest.raises(FileNotFoundError):
            backend.listdir("missing")

    def test_listdir_typed(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess,
            "run",
            make_fake_run(["[f]     a.pdf\n[d]     folder\n"]),
        )
        assert backend.listdir_typed(".") == [("a.pdf", False), ("folder", True)]


class TestRemarkableBackendCd:
    def test_cd_into_directory_updates_path(self, backend, monkeypatch):
        # `ls` sur le dossier cible renvoie plusieurs entrées -> c'est un dossier.
        monkeypatch.setattr(
            file_utils.subprocess,
            "run",
            make_fake_run(["[f]     a.pdf\n[f]     b.pdf\n"]),
        )
        backend.cd("Documents")
        assert backend.path == "/Documents"

    def test_cd_to_file_raises_notadirectory(self, backend, monkeypatch):
        # Une seule entrée, dont le nom == basename de la cible -> fichier.
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["[f]     report.pdf\n"])
        )
        with pytest.raises(NotADirectoryError):
            backend.cd("report.pdf")
        # Le chemin courant ne doit pas avoir bougé.
        assert backend.path == "/"

    def test_cd_missing_raises_filenotfound(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["ERROR: not found\n"])
        )
        with pytest.raises(FileNotFoundError):
            backend.cd("nope")


class TestRemarkableBackendGetPut:
    def test_get_success_creates_dest_dir(self, backend, monkeypatch, tmp_path):
        dest = tmp_path / "downloads"
        monkeypatch.setattr(file_utils.subprocess, "run", make_fake_run(["\n"]))

        backend.get("report.pdf", dest=str(dest))

        assert dest.is_dir()

    def test_get_error_raises_filenotfound(self, backend, monkeypatch, tmp_path):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["ERROR: not found\n"])
        )
        with pytest.raises(FileNotFoundError):
            backend.get("missing.pdf", dest=str(tmp_path / "dl"))

    def test_put_success(self, backend, monkeypatch, tmp_path):
        local_file = tmp_path / "local.pdf"
        local_file.write_text("x")
        fake_run = make_fake_run(["\n"])
        monkeypatch.setattr(file_utils.subprocess, "run", fake_run)

        backend.put(str(local_file), "target")

        assert fake_run.calls[0]["args"][1:] == ["put", str(local_file), "/target"]

    def test_put_error_raises_filenotfound(self, backend, monkeypatch, tmp_path):
        local_file = tmp_path / "local.pdf"
        local_file.write_text("x")
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["ERROR: quota\n"])
        )
        with pytest.raises(FileNotFoundError):
            backend.put(str(local_file), "target")


class TestRemarkableBackendStatIsDir:
    def test_stat_entry_found(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["[f]     a.pdf\n"])
        )
        assert backend.stat_entry("a.pdf") == "a.pdf"

    def test_stat_entry_not_found(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["[f]     a.pdf\n"])
        )
        assert backend.stat_entry("missing.pdf") is None

    def test_is_dir_root_is_always_true(self, backend):
        assert backend.is_dir(".") is True

    def test_is_dir_true_for_folder(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess,
            "run",
            make_fake_run(["[d]     Documents\n[f]     a.pdf\n"]),
        )
        assert backend.is_dir("Documents") is True

    def test_is_dir_false_for_file(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess,
            "run",
            make_fake_run(["[d]     Documents\n[f]     a.pdf\n"]),
        )
        assert backend.is_dir("a.pdf") is False

    def test_is_dir_missing_raises_filenotfound(self, backend, monkeypatch):
        monkeypatch.setattr(
            file_utils.subprocess, "run", make_fake_run(["[f]     a.pdf\n"])
        )
        with pytest.raises(FileNotFoundError):
            backend.is_dir("missing")


class TestRemarkableBackendRateLimitRetry:
    def test_retries_on_429_then_succeeds(self, backend, monkeypatch):
        fake_run = make_fake_run(["429 Too Many Requests\n", "[f]     a.pdf\n"])
        monkeypatch.setattr(file_utils.subprocess, "run", fake_run)

        result = backend.listdir(".")

        assert result == ["a.pdf"]
        assert len(fake_run.calls) == 2

    def test_gives_up_after_max_attempts(self, backend, monkeypatch):
        fake_run = make_fake_run(["429 Too Many Requests\n"])
        monkeypatch.setattr(file_utils.subprocess, "run", fake_run)

        with pytest.raises(FileNotFoundError):
            backend.listdir(".")

        assert len(fake_run.calls) == backend.MAX_RETRY_ATTEMPTS


class TestRemarkableBackendPwd:
    def test_pwd_returns_current_path(self, backend):
        backend.path = "/Notes/Physics"
        assert backend.pwd() == "/Notes/Physics"


class TestRemarkableBackendSync:
    def test_sync_downloads_recursively(self, backend, monkeypatch, tmp_path):
        # Racine : un fichier + un dossier.
        # Sous-dossier : un fichier.
        listing_by_path = {
            "/": [("root.pdf", False), ("Sub", True)],
            "/Sub": [("nested.pdf", False)],
        }

        def fake_listdir_typed(subpath="."):
            target = backend._resolve(subpath)
            return listing_by_path.get(target, [])

        monkeypatch.setattr(backend, "listdir_typed", fake_listdir_typed)
        get_calls = []
        monkeypatch.setattr(
            backend, "get", lambda remote, dest: get_calls.append((remote, dest))
        )

        dest = tmp_path / "sync_out"
        backend.sync(dest=str(dest))

        assert dest.is_dir()
        assert (dest / "Sub").is_dir()
        assert ("/root.pdf", str(dest)) in get_calls
        assert ("/Sub/nested.pdf", str(dest / "Sub")) in get_calls

    def test_sync_skips_files_that_fail(self, backend, monkeypatch, tmp_path):
        monkeypatch.setattr(
            backend, "listdir_typed", lambda subpath=".": [("broken.pdf", False)]
        )

        def failing_get(remote, dest):
            raise FileNotFoundError(remote)

        monkeypatch.setattr(backend, "get", failing_get)

        dest = tmp_path / "sync_out"
        # Ne doit pas lever, juste ignorer le fichier en échec.
        backend.sync(dest=str(dest))
        assert dest.is_dir()


# --------------------------------------------------------------------------- #
# FileManager — navigation locale
# --------------------------------------------------------------------------- #


class TestFileManagerInit:
    def test_starts_in_local_mode(self, local_file_manager):
        assert local_file_manager.mode == FileManager.MODE_LOCAL
        assert local_file_manager.in_remarkable is False
        assert local_file_manager.in_root is False

    def test_no_remarkable_backend_when_rmapi_unset(self, local_file_manager):
        assert local_file_manager.remarkable is None


class TestFileManagerChangeDirectoryLocal:
    def test_cd_into_subdir(self, local_file_manager, project_tree, monkeypatch):
        monkeypatch.chdir(project_tree)
        fm = local_file_manager
        fm.local_location = os.getcwd().replace("\\", "/")

        fm.change_directory("subdir")

        assert os.path.basename(os.getcwd()) == "subdir"

    def test_cd_parent(self, local_file_manager, project_tree, monkeypatch):
        monkeypatch.chdir(project_tree / "subdir")
        fm = local_file_manager
        fm.local_location = os.getcwd().replace("\\", "/")

        fm.change_directory("..")

        assert os.getcwd().replace("\\", "/").rstrip("/") == str(project_tree).replace(
            "\\", "/"
        ).rstrip("/")

    def test_cd_missing_raises_filenotfound(self, local_file_manager):
        with pytest.raises(FileNotFoundError):
            local_file_manager.change_directory("does_not_exist")

    def test_cd_into_file_raises_notadirectory(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(NotADirectoryError):
            local_file_manager.change_directory("report.txt")

    def test_cd_home(self, local_file_manager):
        local_file_manager.change_directory("~")
        assert local_file_manager.mode == FileManager.MODE_LOCAL
        assert os.getcwd().replace("\\", "/") == os.path.expanduser("~").replace(
            "\\", "/"
        )

    def test_cd_remote_without_remarkable_raises(self, local_file_manager):
        with pytest.raises(RemarkableUnavailableError):
            local_file_manager.change_directory("reMarkable:/Notes")


class TestFileManagerRoot:
    def test_slash_switches_to_root_mode(self, local_file_manager):
        local_file_manager.change_directory("/")
        assert local_file_manager.mode == FileManager.MODE_ROOT
        assert local_file_manager.location == "/"

    def test_list_on_root_returns_virtual_entries(self, local_file_manager):
        local_file_manager.change_directory("/")
        entries = local_file_manager.list(".", all=False)
        # Sur une machine sans lecteur Windows monté, seules les entrées
        # statiques doivent apparaître.
        for static_entry in VIRTUAL_ROOT_STATIC_ENTRIES:
            assert static_entry in entries

    def test_list_on_root_with_unknown_path_raises(self, local_file_manager):
        local_file_manager.change_directory("/")
        with pytest.raises(IsVirtualRootError):
            local_file_manager.list("unknown", all=False)

    def test_operations_on_root_raise_virtual_root_error(self, local_file_manager):
        local_file_manager.change_directory("/")
        with pytest.raises(IsVirtualRootError):
            local_file_manager.cat("anything")
        with pytest.raises(IsVirtualRootError):
            local_file_manager.stat("anything")
        with pytest.raises(IsVirtualRootError):
            local_file_manager.find("x", ".", 3, False)
        with pytest.raises(IsVirtualRootError):
            local_file_manager.copy("a", "b", False)

    def test_cd_back_from_root_with_slash(self, local_file_manager):
        local_file_manager.change_directory("/")
        local_file_manager.change_directory("/")
        assert local_file_manager.mode == FileManager.MODE_ROOT

    def test_wsl_path_from_root_attempts_local_routing(self, local_file_manager):
        # Sur une machine non-Windows, ce chemin UNC n'existe pas : on
        # vérifie seulement que le routage tente bien le système de fichiers
        # local (et échoue proprement), pas qu'il réussit.
        local_file_manager.change_directory("/")
        with pytest.raises(FileNotFoundError):
            local_file_manager.change_directory("/wsl-Ubuntu/home/user")

    def test_unknown_drive_letter_from_root_raises_filenotfound(
        self, local_file_manager
    ):
        local_file_manager.change_directory("/")
        with pytest.raises(FileNotFoundError):
            local_file_manager.change_directory("/Z:/some/path")

    def test_virtual_root_entries_helper(self, local_file_manager):
        entries = local_file_manager.virtual_root_entries()
        assert set(VIRTUAL_ROOT_STATIC_ENTRIES).issubset(set(entries))


class TestFileManagerList:
    def test_list_hides_dotfiles_by_default(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        entries = local_file_manager.list(".", all=False)
        assert "report.txt" in entries
        assert ".hidden.txt" not in entries

    def test_list_shows_dotfiles_with_all(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        entries = local_file_manager.list(".", all=True)
        assert ".hidden.txt" in entries

    def test_list_marks_directories_with_trailing_slash(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        entries = local_file_manager.list(".", all=False)
        assert "subdir/" in entries

    def test_list_quotes_names_with_spaces(
        self, local_file_manager, tmp_path, monkeypatch
    ):
        (tmp_path / "a file.txt").write_text("x")
        monkeypatch.chdir(tmp_path)
        entries = local_file_manager.list(".", all=False)
        assert "'a file.txt'" in entries

    def test_list_on_file_raises_notadirectory(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(NotADirectoryError):
            local_file_manager.list("report.txt", all=False)

    def test_list_missing_raises_filenotfound(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(FileNotFoundError):
            local_file_manager.list("does_not_exist", all=False)


class TestFileManagerCat:
    def test_cat_reads_file_contents(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        assert local_file_manager.cat("report.txt") == "root report"

    def test_cat_directory_raises_notafileerror(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(NotAFileError):
            local_file_manager.cat("subdir")

    def test_cat_missing_raises_filenotfound(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(FileNotFoundError):
            local_file_manager.cat("nope.txt")

    def test_cat_on_remarkable_mode_raises_inremarkableerror(self, local_file_manager):
        local_file_manager.mode = FileManager.MODE_REMARKABLE
        with pytest.raises(InReMarkableError):
            local_file_manager.cat("notebook.rm")


class TestFileManagerStat:
    def test_stat_returns_rwx_flags(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        flags = local_file_manager.stat("report.txt")
        assert flags == [
            os.access(project_tree / "report.txt", os.R_OK),
            os.access(project_tree / "report.txt", os.W_OK),
            os.access(project_tree / "report.txt", os.X_OK),
        ]

    def test_stat_missing_raises_filenotfound(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        with pytest.raises(FileNotFoundError):
            local_file_manager.stat("nope.txt")


class TestFileManagerFind:
    def test_find_matches_by_substring(
        self, local_file_manager, project_tree, monkeypatch
    ):
        monkeypatch.chdir(project_tree)
        results = local_file_manager.find("report", str(project_tree), 5, False)
        names = {os.path.basename(p) for p in results}
        assert names == {"report.txt", "report_old.txt"}

    def test_find_strict_requires_exact_match(self, local_file_manager, project_tree):
        results = local_file_manager.find("report", str(project_tree), 5, True)
        assert results == []  # "report" != "report.txt" en fullmatch

        results = local_file_manager.find(
            re.escape("report.txt"), str(project_tree), 5, True
        )
        assert [os.path.basename(p) for p in results] == ["report.txt"]

    def test_find_respects_depth(self, local_file_manager, project_tree):
        # depth=1 : ne doit pas descendre jusqu'à subdir/nested/data.csv
        results = local_file_manager.find("data", str(project_tree), 1, False)
        assert results == []

        results = local_file_manager.find("data", str(project_tree), 5, False)
        assert any("data.csv" in p for p in results)

    def test_find_dir_only(self, local_file_manager, project_tree):
        results = local_file_manager.find("sub/", str(project_tree), 5, False)
        assert [os.path.basename(p) for p in results] == ["subdir"]

    def test_find_empty_pattern_raises_valueerror(
        self, local_file_manager, project_tree
    ):
        with pytest.raises(ValueError):
            local_file_manager.find("", str(project_tree), 5, False)

    def test_find_on_file_raises_notadirectory(self, local_file_manager, project_tree):
        with pytest.raises(NotADirectoryError):
            local_file_manager.find("x", str(project_tree / "report.txt"), 5, False)

    def test_find_missing_path_raises_filenotfound(self, local_file_manager):
        with pytest.raises(FileNotFoundError):
            local_file_manager.find("x", "/path/does/not/exist", 5, False)


class TestFileManagerCopy:
    def test_copy_file(self, local_file_manager, project_tree):
        src = str(project_tree / "report.txt")
        dst = str(project_tree / "report_copy.txt")

        local_file_manager.copy(src, dst, recursive=False)

        assert os.path.isfile(dst)
        with open(dst) as f:
            assert f.read() == "root report"

    def test_copy_directory_recursive(self, local_file_manager, project_tree):
        src = str(project_tree / "subdir")
        dst = str(project_tree / "subdir_copy")

        local_file_manager.copy(src, dst, recursive=True)

        assert os.path.isfile(os.path.join(dst, "report_old.txt"))
        assert os.path.isfile(os.path.join(dst, "nested", "data.csv"))

    def test_copy_directory_without_recursive_raises(
        self, local_file_manager, project_tree
    ):
        src = str(project_tree / "subdir")
        dst = str(project_tree / "subdir_copy")

        with pytest.raises(ValueError):
            local_file_manager.copy(src, dst, recursive=False)

    def test_copy_missing_source_raises_filenotfound(
        self, local_file_manager, project_tree
    ):
        with pytest.raises(FileNotFoundError):
            local_file_manager.copy(
                str(project_tree / "nope.txt"),
                str(project_tree / "dst.txt"),
                recursive=False,
            )


# --------------------------------------------------------------------------- #
# FileManager <-> reMarkable (backend mocké au niveau des méthodes)
# --------------------------------------------------------------------------- #


class TestFileManagerRemarkableNavigation:
    def test_cd_into_remarkable_via_prefix(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.remarkable.cd = MagicMock()

        fm.change_directory("reMarkable:/Notes")

        assert fm.mode == FileManager.MODE_REMARKABLE
        fm.remarkable.cd.assert_called_once_with("/Notes")

    def test_cd_into_remarkable_root_only(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.remarkable.cd = MagicMock()

        fm.change_directory("/reMarkable")

        assert fm.mode == FileManager.MODE_REMARKABLE
        fm.remarkable.cd.assert_not_called()

    def test_failed_remarkable_cd_falls_back_to_local(
        self, remarkable_capable_file_manager
    ):
        fm = remarkable_capable_file_manager
        fm.remarkable.cd = MagicMock(side_effect=FileNotFoundError("nope"))

        with pytest.raises(FileNotFoundError):
            fm.change_directory("reMarkable:/DoesNotExist")

        assert fm.mode == FileManager.MODE_LOCAL

    def test_cd_within_remarkable(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.remarkable.cd = MagicMock()
        fm.change_directory("reMarkable:/Notes")
        fm.mode = FileManager.MODE_REMARKABLE  # déjà positionné par l'appel précédent

        fm.change_directory("Physics")

        fm.remarkable.cd.assert_called_with("Physics")

    def test_list_remarkable(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.listdir = MagicMock(return_value=["a.pdf", "Notes/"])

        entries = fm.list(".", all=False)

        assert entries == ["a.pdf", "Notes/"]

    def test_stat_remarkable_found(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.stat_entry = MagicMock(return_value="a.pdf")

        assert fm.stat("a.pdf") == [True, True, True]

    def test_stat_remarkable_missing_raises(self, remarkable_capable_file_manager):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.stat_entry = MagicMock(return_value=None)

        with pytest.raises(FileNotFoundError):
            fm.stat("missing.pdf")


class TestFileManagerRemarkableCopy:
    def test_copy_local_file_to_remarkable(
        self, remarkable_capable_file_manager, tmp_path
    ):
        fm = remarkable_capable_file_manager
        local_src = tmp_path / "local" / "notes.pdf"
        local_src.write_text("content")
        fm.remarkable.put = MagicMock()

        fm.copy(str(local_src), "reMarkable:/Notes/notes.pdf", recursive=False)

        fm.remarkable.put.assert_called_once()
        args, _ = fm.remarkable.put.call_args
        assert args[0] == str(local_src).replace("\\", "/")
        assert args[1] == "/Notes/notes.pdf"

    def test_copy_remarkable_file_to_local(
        self, remarkable_capable_file_manager, tmp_path
    ):
        fm = remarkable_capable_file_manager
        fm.remarkable.is_dir = MagicMock(return_value=False)
        fm.remarkable.get = MagicMock()

        dst = tmp_path / "downloaded.pdf"
        fm.copy("reMarkable:/Notes/notes.pdf", str(dst), recursive=False)

        fm.remarkable.get.assert_called_once_with(
            "/Notes/notes.pdf", str(dst).replace("\\", "/")
        )

    def test_copy_remarkable_dir_to_local_without_recursive_raises(
        self, remarkable_capable_file_manager, tmp_path
    ):
        fm = remarkable_capable_file_manager
        fm.remarkable.is_dir = MagicMock(return_value=True)

        with pytest.raises(ValueError):
            fm.copy(
                "reMarkable:/Notes",
                str(tmp_path / "Notes_copy"),
                recursive=False,
            )

    def test_copy_remarkable_dir_to_local_recursive(
        self, remarkable_capable_file_manager, tmp_path
    ):
        fm = remarkable_capable_file_manager
        fm.remarkable.is_dir = MagicMock(return_value=True)
        fm.remarkable._resolve = MagicMock(return_value="/Notes")
        sync_calls = []
        fm.remarkable._sync_dir = MagicMock(
            side_effect=lambda remote, local: sync_calls.append((remote, local))
        )

        existing_dst = tmp_path / "existing_dst"
        existing_dst.mkdir()

        fm.copy("reMarkable:/Notes", str(existing_dst), recursive=True)

        # Comme `existing_dst` existe déjà, un sous-dossier "Notes" doit
        # être créé dedans plutôt que de réutiliser `existing_dst` tel quel.
        expected_local = os.path.join(str(existing_dst), "Notes")
        assert sync_calls == [("/Notes", expected_local)]
        assert os.path.isdir(expected_local)

    def test_copy_remarkable_to_remarkable_unsupported(
        self, remarkable_capable_file_manager
    ):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.is_dir = MagicMock(return_value=True)

        with pytest.raises(ValueError):
            fm.copy("SourceDir", "DestDir", recursive=True)

    def test_copy_remarkable_file_to_remarkable(
        self, remarkable_capable_file_manager, tmp_path
    ):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.is_dir = MagicMock(return_value=False)

        downloaded_name = "notes.pdf"

        def fake_get(remote, dest):
            # simule le téléchargement d'un fichier dans le dossier temporaire
            with open(os.path.join(dest, downloaded_name), "w") as f:
                f.write("content")

        fm.remarkable.get = MagicMock(side_effect=fake_get)
        fm.remarkable.put = MagicMock()

        fm.copy("notes.pdf", "Archive/notes.pdf", recursive=False)

        fm.remarkable.get.assert_called_once()
        fm.remarkable.put.assert_called_once()
        put_args, _ = fm.remarkable.put.call_args
        assert put_args[1] == "Archive/notes.pdf"

    def test_copy_remarkable_to_remarkable_missing_source_raises(
        self, remarkable_capable_file_manager
    ):
        fm = remarkable_capable_file_manager
        fm.mode = FileManager.MODE_REMARKABLE
        fm.remarkable.is_dir = MagicMock(return_value=False)
        fm.remarkable.get = MagicMock(side_effect=lambda remote, dest: None)

        with pytest.raises(FileNotFoundError):
            fm.copy("missing.pdf", "Archive/missing.pdf", recursive=False)


class TestFileManagerRemarkableUnavailable:
    def test_operations_raise_when_no_backend_configured(self, local_file_manager):
        with pytest.raises(RemarkableUnavailableError):
            local_file_manager.copy("reMarkable:/a", "b", recursive=False)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
