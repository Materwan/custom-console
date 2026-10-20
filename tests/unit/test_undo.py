from __future__ import annotations

import os
import stat
import sys

import pytest

from custom_console.core.undo import UndoLog, bytes_hash

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes in the fixtures")


@pytest.fixture
def log(tmp_path):
    return UndoLog(tmp_path / "undo"), tmp_path / "work"


def change(undo, path, data: bytes):
    """What a write tool does: record, write, record the result."""
    number = undo.before(str(path))
    path.write_bytes(data)
    undo.after(number, data)


def test_an_edit_is_undone_and_the_mode_comes_back(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    os.chmod(f, 0o640)
    undo.begin_turn("t1")
    change(undo, f, b"changed")
    os.chmod(f, 0o600)
    report = undo.undo_last()
    assert f.read_text() == "original" and report.restored == [str(f)] and report.clean
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    assert undo.undo_last().nothing


def test_a_file_the_console_created_is_deleted_with_the_folders_it_made(log):
    undo, work = log
    work.mkdir()
    undo.begin_turn("t1")
    sub = work / "new" / "deeper"
    number = undo.before(str(sub / "f.txt"), created_dirs=[str(work / "new"), str(sub)])
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x")
    undo.after(number, b"x")
    report = undo.undo_last()
    assert report.deleted == [str(sub / "f.txt")] and not (work / "new").exists() and work.exists()


def test_move_then_edit_is_not_lost(log):
    """BUG-5: the original must come back after a sequence of changes in one turn, in reverse order."""
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("ORIGINAL")
    undo.begin_turn("t1")
    change(undo, f, b"FIRST")
    change(undo, f, b"SECOND")
    change(undo, f, b"THIRD")
    report = undo.undo_last()
    assert f.read_text() == "ORIGINAL" and len(report.restored) == 3 and report.clean


def test_a_file_changed_by_someone_else_is_left_alone_and_the_record_is_kept(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    change(undo, f, b"by the console")
    f.write_text("by the person afterwards")  # BUG-5: a later edit must not be overwritten
    report = undo.undo_last()
    assert f.read_text() == "by the person afterwards"
    assert report.skipped and "changed since" in report.skipped[0][1] and not report.restored
    assert undo.last_turn_records(), "the record stays until the file is restored"
    f.write_text("by the console")  # if the content matches again, the undo can go through
    assert undo.undo_last().restored


def test_a_file_removed_since_is_skipped_not_recreated(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    change(undo, f, b"new")
    f.unlink()
    report = undo.undo_last()
    assert not f.exists() and report.skipped and "removed" in report.skipped[0][1]


def test_a_failed_restore_keeps_the_record_and_names_the_file(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    change(undo, f, b"new")
    os.chmod(work, 0o500)  # the folder cannot take the temporary file
    try:
        report = undo.undo_last()
    finally:
        os.chmod(work, 0o700)
    assert report.failed and report.failed[0][0] == str(f) and f.read_text() == "new"
    assert undo.last_turn_records()
    assert undo.undo_last().restored and f.read_text() == "original"  # and it works once the cause is gone


def test_a_crash_never_leaves_a_half_written_file(log, monkeypatch):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    change(undo, f, b"new")
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("power cut")))
    report = undo.undo_last()
    monkeypatch.setattr(os, "replace", real_replace)
    assert f.read_text() == "new" and report.failed and not [p for p in work.iterdir() if p.name.startswith(".undo-")]


def test_a_write_that_did_not_finish_is_only_undone_if_the_file_is_untouched(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    undo.before(str(f))  # no `after`: the process died mid-write
    assert undo.undo_last().unchanged == [str(f)]
    undo.begin_turn("t2")
    undo.before(str(f))
    f.write_text("half")
    assert undo.undo_last().skipped


def test_a_discarded_record_leaves_nothing_behind(log):
    undo, work = log
    work.mkdir()
    f = work / "a.txt"
    f.write_text("original")
    undo.begin_turn("t1")
    number = undo.before(str(f))
    undo.discard(number)
    assert undo.pending_changes == 0 and undo.undo_last().nothing
    assert not list((undo.root).glob("*/*.bak"))


def test_only_the_last_turn_is_undone_and_older_turns_stay_for_later(log):
    undo, work = log
    work.mkdir()
    a, b = work / "a", work / "b"
    a.write_text("a0")
    b.write_text("b0")
    undo.begin_turn("t1")
    change(undo, a, b"a1")
    undo.begin_turn("t2")
    change(undo, b, b"b1")
    assert undo.undo_last().restored == [str(b)] and a.read_text() == "a1"
    assert undo.undo_last().restored == [str(a)] and a.read_text() == "a0"


def test_only_twenty_turns_are_kept(tmp_path):
    undo = UndoLog(tmp_path / "undo", keep_turns=3)
    work = tmp_path / "w"
    work.mkdir()
    for i in range(6):
        undo.begin_turn(f"t{i}")
        change(undo, work / f"f{i}", b"x")
    assert undo.prune() == 3 and len(list((tmp_path / "undo").iterdir())) == 3


def test_only_regular_files_can_be_recorded(log):
    undo, work = log
    work.mkdir()
    (work / "d").mkdir()
    undo.begin_turn("t1")
    with pytest.raises(OSError, match="not a regular file"):
        undo.before(str(work / "d"))


def test_hash_helper():
    assert bytes_hash(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
