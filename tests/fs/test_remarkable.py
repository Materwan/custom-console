"""Unit tests for RemarkableBackend (subprocess and sleeping are mocked)."""

from __future__ import annotations

import subprocess

import pytest

from custom_console.fs import RemarkableBackend, RemarkableError, UnsafeOperationError
from custom_console.fs import remarkable as remarkable_module
from custom_console.fs.remarkable import first_error_line, is_rate_limited, parse_listing


class FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


def make_fake_run(script):
    """Fake `subprocess.run` replaying `script` (one combined output per call).

    The last item is reused once the list is exhausted.
    """
    calls = []

    def fake_run(args, capture_output=True, encoding="utf-8", errors="replace", cwd=None, timeout=None):
        calls.append({"args": list(args), "cwd": cwd, "timeout": timeout})
        index = min(len(calls) - 1, len(script) - 1)
        return FakeCompletedProcess(stdout=script[index])

    fake_run.calls = calls
    return fake_run


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(remarkable_module.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def backend():
    b = RemarkableBackend("fake_rmapi.exe")
    b.MIN_CALL_INTERVAL = 0.0
    return b


def run_with(monkeypatch, *outputs):
    fake = make_fake_run(list(outputs))
    monkeypatch.setattr(remarkable_module.subprocess, "run", fake)
    return fake


class TestParsing:
    def test_separates_files_and_dirs(self):
        output = "[f]     notes.txt\n[d]     Documents\n[f]     trailing space.pdf \n"
        assert parse_listing(output) == [
            ("notes.txt", False),
            ("Documents", True),
            ("trailing space.pdf ", False),
        ]

    def test_ignores_blank_and_unknown_lines(self):
        output = "\n[f]     a.txt\n\n   \nnoise\n[d]     b\n"
        assert parse_listing(output) == [("a.txt", False), ("b", True)]

    def test_error_lines_are_not_entries(self):
        assert parse_listing("ERROR: something went wrong\n") == []

    def test_tab_separated_listing(self):
        assert parse_listing("[d]\tFolder\n[f]\tFile\n") == [("Folder", True), ("File", False)]

    def test_document_named_error_is_not_an_error(self):
        output = "[f]     ERROR report\n[f]     Invoice 429\n"
        assert first_error_line(output) is None
        assert not is_rate_limited(output)

    def test_detects_errors_and_rate_limit(self):
        assert first_error_line("ERROR: nope\n") == "ERROR: nope"
        assert is_rate_limited("429 Too Many Requests\n")


class TestResolve:
    def test_relative(self, backend):
        backend.path = "/Notes"
        assert backend.resolve("sub") == "/Notes/sub"

    def test_dot_and_empty(self, backend):
        backend.path = "/Notes"
        assert backend.resolve(".") == "/Notes"
        assert backend.resolve("") == "/Notes"

    def test_absolute(self, backend):
        backend.path = "/Notes"
        assert backend.resolve("/Other") == "/Other"

    def test_parent(self, backend):
        backend.path = "/Notes/sub"
        assert backend.resolve("..") == "/Notes"

    def test_root(self, backend):
        assert backend.resolve(".") == "/"


class TestListing:
    def test_listdir(self, backend, monkeypatch):
        fake = run_with(monkeypatch, "[f]     a.pdf\n[d]     folder\n")
        assert backend.listdir(".") == ["a.pdf", "folder/"]
        assert fake.calls[0]["args"][1:] == ["ls", "/"]
        assert fake.calls[0]["timeout"] == backend.COMMAND_TIMEOUT

    def test_missing_raises_filenotfound(self, backend, monkeypatch):
        run_with(monkeypatch, "ERROR: no such node\n")
        with pytest.raises(FileNotFoundError):
            backend.listdir("missing")

    def test_other_errors_raise_remarkable_error(self, backend, monkeypatch):
        run_with(monkeypatch, "ERROR: failed to authenticate\n")
        with pytest.raises(RemarkableError, match="authenticate"):
            backend.listdir(".")

    def test_scandir_typed(self, backend, monkeypatch):
        run_with(monkeypatch, "[f]     a.pdf\n[d]     folder\n")
        assert backend.scandir(".") == [("a.pdf", False), ("folder", True)]

    def test_timeout_is_reported(self, backend, monkeypatch):
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("rmapi", 1)

        monkeypatch.setattr(remarkable_module.subprocess, "run", boom)
        with pytest.raises(TimeoutError, match="timed out"):
            backend.listdir(".")


class TestCd:
    def test_into_directory(self, backend, monkeypatch):
        run_with(monkeypatch, "[f]     a.pdf\n[f]     b.pdf\n")
        backend.cd("Documents")
        assert backend.path == "/Documents"

    def test_into_file_is_refused(self, backend, monkeypatch):
        run_with(monkeypatch, "[f]     report.pdf\n")
        with pytest.raises(NotADirectoryError):
            backend.cd("report.pdf")
        assert backend.path == "/"

    def test_folder_with_one_same_named_subfolder_is_a_folder(self, backend, monkeypatch):
        run_with(monkeypatch, "[d]     Notes\n")
        backend.cd("Notes")
        assert backend.path == "/Notes"

    def test_missing(self, backend, monkeypatch):
        run_with(monkeypatch, "ERROR: not found\n")
        with pytest.raises(FileNotFoundError):
            backend.cd("nope")


class TestTransfers:
    def test_get_creates_dest_and_runs_in_it(self, backend, monkeypatch, tmp_path):
        dest = tmp_path / "downloads"
        fake = run_with(monkeypatch, "\n")
        backend.get("report.pdf", dest=str(dest))
        assert dest.is_dir()
        assert fake.calls[0]["cwd"] == str(dest)
        assert fake.calls[0]["args"][1:] == ["get", "/report.pdf"]

    def test_get_missing(self, backend, monkeypatch, tmp_path):
        run_with(monkeypatch, "ERROR: file doesn't exist\n")
        with pytest.raises(FileNotFoundError):
            backend.get("missing.pdf", dest=str(tmp_path / "dl"))

    def test_put(self, backend, monkeypatch, tmp_path):
        local = tmp_path / "local.pdf"
        local.write_text("x")
        fake = run_with(monkeypatch, "\n")
        backend.put(str(local), "target")
        assert fake.calls[0]["args"][1:] == ["put", str(local), "/target"]

    def test_put_error_is_not_reported_as_missing(self, backend, monkeypatch, tmp_path):
        local = tmp_path / "local.pdf"
        local.write_text("x")
        run_with(monkeypatch, "ERROR: quota exceeded\n")
        with pytest.raises(RemarkableError, match="quota"):
            backend.put(str(local), "target")


class TestIsDir:
    def test_root(self, backend):
        assert backend.is_dir(".") is True

    def test_folder_and_file(self, backend, monkeypatch):
        run_with(monkeypatch, "[d]     Documents\n[f]     a.pdf\n")
        assert backend.is_dir("Documents") is True
        assert backend.is_dir("a.pdf") is False

    def test_missing(self, backend, monkeypatch):
        run_with(monkeypatch, "[f]     a.pdf\n")
        with pytest.raises(FileNotFoundError):
            backend.is_dir("missing")


class TestRateLimit:
    def test_retries_then_succeeds(self, backend, monkeypatch):
        fake = run_with(monkeypatch, "429 Too Many Requests\n", "[f]     a.pdf\n")
        assert backend.listdir(".") == ["a.pdf"]
        assert len(fake.calls) == 2

    def test_gives_up_after_max_attempts(self, backend, monkeypatch):
        fake = run_with(monkeypatch, "429 Too Many Requests\n")
        with pytest.raises(RemarkableError, match="429"):
            backend.listdir(".")
        assert len(fake.calls) == backend.MAX_RETRY_ATTEMPTS

    def test_document_named_429_does_not_retry(self, backend, monkeypatch):
        fake = run_with(monkeypatch, "[f]     Invoice 429\n")
        assert backend.listdir(".") == ["Invoice 429"]
        assert len(fake.calls) == 1


class TestRemove:
    def test_refuses_root(self, backend):
        with pytest.raises(UnsafeOperationError):
            backend.remove("/", recursive=True)

    def test_simple_remove(self, backend, monkeypatch):
        fake = run_with(monkeypatch, "\n")
        backend.remove("old.pdf")
        assert fake.calls[0]["args"][1:] == ["rm", "/old.pdf"]

    def test_remove_error(self, backend, monkeypatch):
        run_with(monkeypatch, "ERROR: entry is not empty\n")
        with pytest.raises(RemarkableError, match="not empty"):
            backend.remove("Folder")

    def test_recursive_removes_children_first(self, backend, monkeypatch):
        listings = {"/": [("Folder", True)], "/Folder": [("a.pdf", False), ("Sub", True)], "/Folder/Sub": [("b.pdf", False)]}
        removed = []
        monkeypatch.setattr(backend, "scandir", lambda p=".": listings.get(backend.resolve(p), []))
        monkeypatch.setattr(backend, "_remove_one", removed.append)

        backend.remove("/Folder", recursive=True)

        assert removed == ["/Folder/a.pdf", "/Folder/Sub/b.pdf", "/Folder/Sub", "/Folder"]


class TestDownloadTree:
    def test_downloads_recursively(self, backend, monkeypatch, tmp_path):
        listings = {"/": [("root.pdf", False), ("Sub", True)], "/Sub": [("nested.pdf", False)]}
        monkeypatch.setattr(backend, "scandir", lambda p=".": listings.get(backend.resolve(p), []))
        gets = []
        monkeypatch.setattr(backend, "get", lambda remote, dest: gets.append((remote, dest)))

        dest = tmp_path / "out"
        seen = []
        count = backend.sync(dest, on_file=seen.append)

        assert count == 2
        assert (dest / "Sub").is_dir()
        assert ("/root.pdf", str(dest)) in gets
        assert ("/Sub/nested.pdf", str(dest / "Sub")) in gets
        assert seen == ["/root.pdf", "/Sub/nested.pdf"]

    def test_failures_are_skipped_and_recorded(self, backend, monkeypatch, tmp_path):
        monkeypatch.setattr(backend, "scandir", lambda p=".": [("broken.pdf", False)])

        def failing_get(remote, dest):
            raise FileNotFoundError(remote)

        monkeypatch.setattr(backend, "get", failing_get)

        assert backend.sync(tmp_path / "out") == 0
        assert backend.failed_downloads == ["/broken.pdf"]
