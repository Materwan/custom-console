from __future__ import annotations

import json
import os

from custom_console.core.journal import MAX_BYTES, Journal, prune_journals
from custom_console.core.session import Session, SessionStore, folder_hash, timezone_name
from custom_console.core.tools.base import Todo

CID = "c0123456789abcdef"


def make(tmp_path, n=0, clock=None):
    clock = clock or iter(range(1000, 100000)).__next__
    store = SessionStore(tmp_path / "sessions", "/home/me/work", clock=lambda: float(clock()))
    session = Session(folder="/home/me/work", conversation=f"c{n:016x}", title=f"conversation {n}")
    return store, session


def test_a_session_file_holds_metadata_only(tmp_path):  # SEC-7, SEC-9
    store, session = make(tmp_path)
    session.model = "fake:small"
    session.disabled_tools = {"local_run"}
    session.todos.items = [Todo("a", "completed"), Todo("b")]
    session.prompt_tokens, session.completion_tokens = 120, 30
    session.plan_mode, session.tainted = True, True
    session.pending_notes = ["$ cat secrets\nTOPSECRET"]
    assert store.save(session)
    data = json.loads((store.folder / f"{session.id}.json").read_text())
    assert set(data) == {"id", "conversation", "title", "model", "incognito", "local_only", "project", "packs", "disabled_tools",
                         "todos", "prompt_tokens", "completion_tokens", "created", "updated"}
    text = json.dumps(data)
    assert "TOPSECRET" not in text and "plan_mode" not in text and "trust" not in text and "rule" not in text


def test_restoring_cannot_raise_anything(tmp_path):  # SEC-7
    store, session = make(tmp_path)
    session.plan_mode = True
    store.save(session)
    (store.folder / f"{session.id}.json").write_text(json.dumps({
        **json.loads((store.folder / f"{session.id}.json").read_text()), "permission_level": 2, "plan_mode": False,
        "trusted": True, "rules": [{"effect": "allow"}]}))
    restored = store.list()[0]
    assert restored.plan_mode is False and not hasattr(restored, "permission_level") and not hasattr(restored, "trusted")
    assert restored.disabled_tools == set() and restored.conversation == session.conversation


def test_round_trip(tmp_path):
    store, session = make(tmp_path)
    session.model, session.project, session.packs = "m:x", 4, ["web"]
    session.todos.items = [Todo("task", "in_progress")]
    session.disabled_tools = {"local_write"}
    store.save(session)
    back = store.list()[0]
    assert (back.id, back.conversation, back.title, back.model, back.project, back.packs) == (
        session.id, session.conversation, session.title, "m:x", 4, ["web"])
    assert back.disabled_tools == {"local_write"} and [(t.content, t.status) for t in back.todos.items] == [("task", "in_progress")]


def test_only_the_newest_five_of_a_folder_are_kept_and_listed(tmp_path):
    store, _ = make(tmp_path)
    sessions = []
    for n in range(8):
        _, session = make(tmp_path, n)
        store.save(session)
        sessions.append(session)
    listed = store.list()
    assert [s.title for s in listed] == [f"conversation {n}" for n in (7, 6, 5, 4, 3)]
    assert len(list(store.folder.glob("*.json"))) == 5
    # touching an old one brings it back to the front
    store.save(listed[-1])
    assert store.list()[0].id == listed[-1].id


def test_other_folders_do_not_mix(tmp_path):
    a = SessionStore(tmp_path / "s", "/home/me/a")
    b = SessionStore(tmp_path / "s", "/home/me/b")
    a.save(Session(folder="/home/me/a", conversation=CID, title="A"))
    assert [s.title for s in a.list()] == ["A"] and b.list() == []
    assert folder_hash("/home/me/a") != folder_hash("/home/me/b")
    assert folder_hash("/Home/Me/A", True) == folder_hash("/home/me/a", True)


def test_an_incognito_or_empty_session_is_not_saved(tmp_path):
    store, session = make(tmp_path)
    session.incognito = True
    assert not store.save(session) and not store.folder.exists()
    fresh = Session(folder="/x")
    assert not store.save(fresh)  # no conversation yet


def test_garbage_files_are_ignored_and_saves_are_atomic(tmp_path):
    store, session = make(tmp_path)
    store.save(session)
    (store.folder / "junk.json").write_text("{not json")
    (store.folder / "other.json").write_text(json.dumps({"no": "conversation"}))
    assert [s.id for s in store.list()] == [session.id]
    assert not [p for p in store.folder.iterdir() if p.name.startswith(".session-")]
    store.forget(session)
    assert store.list() == []


def test_timezone_name_is_an_iana_name_or_none():
    name = timezone_name()
    assert name is None or "/" in name or name == "UTC"


# ---------------------------------------------------------------------- journal
def test_journal_redacts_before_writing_and_never_holds_message_text(tmp_path):  # SEC-9
    journal = Journal(tmp_path / "j", "s1", clock=lambda: 5.0)
    journal.tool("local_run", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' x", "env": {"API_KEY": "k"}},
                 "ask", "default", "Needs your answer.", "allow_once", True, 0.5, 100)
    journal.turn("t1", CID, "m", "ok", 1.234, 42)
    journal.approval(3, "ws_delete", "refuse_once", "console")
    journal.error("timeout", "too slow clara_ab12cd_0123456789abcdefghijklmnop")
    text = journal.path.read_text()
    assert "Bearer abcdefghijklmnop" not in text and "clara_ab12cd" not in text and '"API_KEY": "k"' not in text
    assert '"API_KEY": "[redacted]"' in text
    kinds = [e["kind"] for e in journal.read()]
    assert kinds == ["tool", "turn", "approval", "error"]
    assert journal.read()[1]["message_chars"] == 42 and "message" not in journal.read()[1]


def test_long_fields_are_shortened(tmp_path):
    journal = Journal(tmp_path, "s")
    journal.write("tool", content="x" * 50_000)
    assert len(journal.read()[0]["content"]) < 5_000


def test_the_journal_rotates_when_it_is_too_big(tmp_path):
    journal = Journal(tmp_path, "s")
    journal.path.write_text("x" * MAX_BYTES)
    journal.write("turn", id="t")
    assert journal.path.with_suffix(".jsonl.1").exists() and len(journal.read()) == 1


def test_old_journals_are_pruned(tmp_path):
    for i in range(6):
        path = tmp_path / f"s{i}.jsonl"
        path.write_text("{}\n")
        os.utime(path, (1000 + i, 1000 + i))
    assert prune_journals(tmp_path, keep=4) == 2
    assert sorted(p.name for p in tmp_path.glob("*.jsonl")) == ["s2.jsonl", "s3.jsonl", "s4.jsonl", "s5.jsonl"]


def test_a_journal_that_cannot_be_written_never_stops_the_work(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    Journal(blocker / "sub", "s").write("turn", id="t")  # the folder cannot be made: no exception
