from __future__ import annotations

import json
import os
import sys
import time

import pytest

from custom_console.core.vfs.backend import (
    AlreadyExists,
    BackendFailure,
    DirectoryNotEmpty,
    NotFound,
    Unsupported,
)
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.remarkable import (
    RemarkableBackend,
    RmapiRunner,
    classify,
    find_rmapi,
    parse_listing,
    status_lines,
)
from custom_console.platform import Cancelled, CancelToken
from tests.support.harness import sandbox_resolver

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_rmapi.py")]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")

TREE = {"/Notes": {"type": "d"}, "/Notes/Plan": {"type": "f"}, "/Notes/Sub": {"type": "d"}, "/Root doc": {"type": "f"}}


class Tablet:
    def __init__(self, tmp_path, mode="ok", **extra):
        self.tree = tmp_path / "tree.json"
        self.tree.write_text(json.dumps(TREE))
        self.log = tmp_path / "calls.log"
        self.state = tmp_path / "state"
        self.state.mkdir()
        env = {**os.environ, "FAKE_RMAPI_TREE": str(self.tree), "FAKE_RMAPI_MODE": mode, "FAKE_RMAPI_LOG": str(self.log),
               "FAKE_RMAPI_STATE": str(self.state), **extra}
        self.runner = RmapiRunner(FAKE, env=env, min_interval=0.0, first_backoff=0.05, timeout=5)
        self.backend = RemarkableBackend(self.runner, temp_root=str(tmp_path))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def tree_now(self):
        return json.loads(self.tree.read_text())


@pytest.fixture
def tablet(tmp_path):
    return Tablet(tmp_path)


def test_listing_and_stat(tablet):
    b = tablet.backend
    assert sorted((e.name, e.is_dir) for e in b.list("/")) == [("Notes", True), ("Root doc", False)]
    assert sorted(e.name for e in b.list("/Notes")) == ["Plan", "Sub"]
    assert b.stat("/").is_dir and b.stat("/Notes").is_dir and not b.stat("/Notes/Plan").is_dir
    assert b.stat("/Notes/Nope") is None and b.stat("/Nope/Deeper") is None
    with pytest.raises(NotFound):
        b.list("/Nope")


def test_listing_is_cached_and_any_change_clears_it(tablet):
    b = tablet.backend
    b.list("/Notes")
    b.list("/Notes")
    assert len([c for c in tablet.calls() if c[0] == "ls"]) == 1
    b.mkdir("/Notes/New")
    assert "New" in [e.name for e in b.list("/Notes")]
    assert len([c for c in tablet.calls() if c[:2] == ["ls", "/Notes"]]) == 2


def test_names_are_kept_verbatim_including_trailing_spaces():
    assert [e.name for e in parse_listing("[f]\tDoc with space \n[d]\tFolder\n")] == ["Doc with space ", "Folder"]
    assert parse_listing("rubbish\n[x] nope") == []


def test_errors_are_judged_from_status_lines_only(tablet):  # BUG-17
    listing = "[f]\tERROR 429 report\n[d]\tTrying 429 times\n"
    assert classify(listing, 0) is None
    assert status_lines(listing) == []
    assert classify("ERROR: failed to fetch tree: HTTP 429 Too Many Requests", 1).kind == "ratelimit"
    assert classify(listing + "ERROR: directory doesn't exist /x", 1).kind == "notfound"


@pytest.mark.parametrize(("output", "code", "kind"), [
    ("Enter one-time code (go to https://my.remarkable.com/device/desktop/connect): ", 1, "signin"),
    ("ERROR: file doesn't exist /x", 1, "notfound"), ("ERROR: entry not found", 1, "notfound"),
    ("ERROR: directory is not empty", 1, "notempty"), ("ERROR: entry already exists /x", 1, "exists"),
    ("ERROR: Get \"https://x\": dial tcp: lookup x: no such host", 1, "unreachable"),
    ("ERROR: something odd", 1, "failed"), ("boom", 3, "failed"), ("fine", 0, None), ("", 0, None),
])
def test_classification(output, code, kind):
    found = classify(output, code)
    assert (found.kind if found else None) == kind


def test_not_signed_in_does_not_wait_for_a_code(tmp_path):  # BUG-18
    tablet = Tablet(tmp_path, "unauth")
    begun = time.monotonic()
    with pytest.raises(BackendFailure, match="not signed in"):
        tablet.backend.list("/")
    assert time.monotonic() - begun < 5


def test_a_hanging_rmapi_is_stopped_at_the_timeout(tmp_path):
    tablet = Tablet(tmp_path, "hang")
    tablet.runner.timeout = 1.0
    begun = time.monotonic()
    with pytest.raises(BackendFailure, match="did not answer"):
        tablet.backend.list("/")
    assert time.monotonic() - begun < 8


def test_rate_limits_are_retried_with_a_delay_then_succeed(tmp_path):
    tablet = Tablet(tmp_path, "rate", FAKE_RMAPI_RATE="2")
    assert sorted(e.name for e in tablet.backend.list("/Notes")) == ["Plan", "Sub"]
    assert len(tablet.calls()) == 3


def test_rate_limiting_that_never_ends_is_reported(tmp_path):
    tablet = Tablet(tmp_path, "rate", FAKE_RMAPI_RATE="99")
    tablet.runner.retries = 3
    with pytest.raises(BackendFailure, match="still limiting"):
        tablet.backend.list("/")
    assert len(tablet.calls()) == 3


def test_the_cloud_being_unreachable_and_rmapi_missing(tmp_path):
    with pytest.raises(BackendFailure, match="cannot be reached"):
        Tablet(tmp_path, "error").backend.list("/")
    runner = RmapiRunner(["/definitely/not/rmapi"], min_interval=0)
    with pytest.raises(BackendFailure, match="not installed"):
        RemarkableBackend(runner).list("/")


def test_cancel_interrupts_a_wait_and_a_call(tmp_path):
    tablet = Tablet(tmp_path, "rate", FAKE_RMAPI_RATE="99")
    tablet.runner.first_backoff = 30.0
    token = CancelToken()
    import threading
    threading.Timer(0.5, token.cancel).start()
    begun = time.monotonic()
    with pytest.raises(Cancelled):
        tablet.backend.list("/", token)
    assert time.monotonic() - begun < 8


def test_the_spinner_hook_sees_a_label_while_a_call_runs(tablet):
    seen = []
    tablet.runner.activity = seen.append
    tablet.backend.list("/Notes")
    assert seen[0].startswith("rmapi ls") and seen[-1] is None


def test_read_downloads_the_archive_and_cleans_up(tmp_path, tablet):
    data = tablet.backend.read("/Notes/Plan")
    assert data == b"PK-fake" and tablet.backend.read("/Notes/Plan", 2) == b"PK"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("console-rm-")]
    assert tablet.backend.export_name("/Notes/Plan") == "Plan.rmdoc"
    with pytest.raises(NotFound):
        tablet.backend.read("/Notes/Nope")


def test_write_uploads_a_new_document_named_after_the_destination(tablet):
    tablet.backend.write("/Notes/Report.pdf", b"%PDF-1.4 fake")
    assert "/Notes/Report" in tablet.tree_now()
    tablet.backend.write("/Notes/Archive", b"PK\x03\x04...")
    assert "/Notes/Archive" in tablet.tree_now()
    with pytest.raises(Unsupported, match="PDF, EPUB"):
        tablet.backend.write("/Notes/Text", b"plain text")
    with pytest.raises(AlreadyExists):
        tablet.backend.write("/Notes/Sub", b"%PDF")  # a folder


def test_replacing_a_document_uploads_first_and_removes_the_old_one_after(tablet):
    tablet.backend.write("/Notes/Plan.pdf", b"%PDF-new")
    verbs = [c[0] for c in tablet.calls() if c[0] in ("put", "rm", "mv")]
    assert verbs == ["put", "rm", "mv"], "the new copy must be up before the old one goes"
    assert "/Notes/Plan" in tablet.tree_now() and not [p for p in tablet.tree_now() if "upload" in p]


def test_mkdir_rename_remove_rmdir(tablet):
    b = tablet.backend
    b.mkdir("/A/B/C", parents=True)
    assert {"/A", "/A/B", "/A/B/C"} <= set(tablet.tree_now())
    with pytest.raises(AlreadyExists):
        b.mkdir("/A")
    b.rename("/Root doc", "/Notes")
    assert "/Notes/Root doc" in tablet.tree_now()
    with pytest.raises(DirectoryNotEmpty):
        b.rmdir("/Notes")
    b.remove("/Notes/Plan")
    b.rmdir("/A/B/C")
    with pytest.raises(NotFound):
        b.remove("/Nowhere")
    with pytest.raises(Unsupported):
        b.touch("/x")


def test_configuration():
    assert find_rmapi("/definitely/not/there") is None
    assert find_rmapi(sys.executable) == [sys.executable]


# ---------------------------------------------------------------------- through the virtual file system
def test_the_tablet_through_the_vfs(tmp_path, tablet):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    vfs = Vfs(sandbox_resolver(tmp_path, home, remarkable=tablet.backend), cwd=str(work))
    assert [e.name for e in vfs.list(vfs.resolve("reMarkable:Notes"))] == ["Plan", "Sub"]
    vfs.chdir("reMarkable:Notes")
    assert vfs.cwd == "reMarkable:/Notes" and [e.name for e in vfs.list(vfs.here())] == ["Plan", "Sub"]
    vfs.chdir("Sub")
    assert vfs.cwd == "reMarkable:/Notes/Sub"
    # a document taken off the tablet is named after its archive
    report = vfs.copy(vfs.resolve("reMarkable:/Notes/Plan"), vfs.resolve(str(work)))
    assert report.ok and (work / "Plan.rmdoc").read_bytes() == b"PK-fake"
    # a PDF put on the tablet
    (work / "paper.pdf").write_bytes(b"%PDF-1.7 paper")
    assert vfs.copy(vfs.resolve(str(work / "paper.pdf")), vfs.resolve("reMarkable:/Notes")).ok
    assert "/Notes/paper" in tablet.tree_now()
    # tablet to tablet uses rmapi mv, not a download and an upload
    tablet.backend.forget()
    before = len(tablet.calls())
    vfs.move(vfs.resolve("reMarkable:/Notes/paper"), vfs.resolve("reMarkable:/Notes/Sub"))
    assert [c[0] for c in tablet.calls()[before:] if c[0] in ("get", "put")] == []
    assert "/Notes/Sub/paper" in tablet.tree_now()
