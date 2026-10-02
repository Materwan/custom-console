"""Saved sessions: the store, the records, and /restore through a real console."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from test_console import Session, wait_for

from custom_console.agent.sessions import SessionRecord, SessionStore, ago, directory_key
from custom_console.agent.turn import TurnStats, TurnView


def turn(prompt="question", answer="answer"):
    view = TurnView(prompt)
    view.add_text(answer)
    view.stats = TurnStats(1, 2, 3, 1.5, 10.0)
    return view.to_dict()


class TestTurnData:
    def test_a_turn_survives_being_saved_as_json(self):
        view = TurnView("do it")
        view.add_text("Working. ")
        note = view.tool_started("file_system_read", {"path": "a.py"})
        view.tool_finished(
            note, "file_system_read", {"path": "a.py"}, True, 0.2, detail="@@ -1 +1 @@\n-a\n+b", detail_kind="diff"
        )
        view.show_todos("◐ step")
        view.add_text("Done.")
        view.stats = TurnStats(10, 20, 30, 2.0, 55.0)
        data = json.loads(json.dumps(view.to_dict()))
        again = TurnView.from_dict(data)
        assert again.to_dict() == view.to_dict()
        assert again.stats == view.stats and again.prompt == "do it" and again.todos == "◐ step"
        assert again.snapshot() == view.snapshot()

    def test_older_sessions_with_diff_and_checklist_segments_still_load(self):
        data = {
            "prompt": "p",
            "segments": [
                {"kind": "note", "text": "✔ file_system_edit(path='a') · 0.1s", "style": "tool"},
                {"kind": "diff", "text": "-a\n+b"},
                {"kind": "todo", "text": "☑ step"},
            ],
        }
        view = TurnView.from_dict(data)
        assert [s.kind for s in view.snapshot()] == ["note", "diff"] and view.todos == "☑ step"

    def test_odd_data_does_not_crash(self):
        view = TurnView.from_dict({"segments": [{"text": "x"}], "stats": {"input_tokens": 3, "bogus": 1}})
        assert view.prompt == "" and view.snapshot()[0].text == "x" and view.stats.input_tokens == 3


class TestRecord:
    def test_roundtrip_and_tolerance(self):
        record = SessionRecord(
            "id1", "/work", model="m", permission_level=2, disabled_tools=["a"],
            todos=[{"content": "x", "status": "pending"}], context={"base": "b", "generation": 2, "summary": "s"},
            turns=[turn("first line\nsecond"), turn()],
        )
        again = SessionRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        assert again == record
        broken = SessionRecord.from_dict({"id": "x", "turns": "nope", "todos": [1, {"content": "a"}], "context": 5})
        assert broken.turns == [] and broken.todos == [{"content": "a"}] and broken.context == {}

    def test_first_prompt_is_one_clipped_line(self):
        assert SessionRecord("i", "/d").first_prompt == ""
        assert SessionRecord("i", "/d", turns=[turn("first line\nsecond")]).first_prompt == "first line"
        assert len(SessionRecord("i", "/d", turns=[turn("x" * 200)]).first_prompt) == 70

    def test_the_conversation_on_the_server(self):
        assert SessionRecord("i", "/d", context={"conversation": "c", "summary": "s"}).conversation_id() == "c"
        assert SessionRecord("i", "/d", context={"base": "b", "generation": 2}).conversation_id() == "b"  # older versions
        assert SessionRecord("i", "/d").conversation_id() == ""

    def test_ago(self):
        now = datetime.now().astimezone()
        assert ago(now) == "just now"
        assert ago(now - timedelta(minutes=5)) == "5 min ago"
        assert ago(now - timedelta(hours=3)) == "3 h ago"
        assert ago(now - timedelta(days=1)) == "1 day ago" and ago(now - timedelta(days=4)) == "4 days ago"


class TestStore:
    def test_save_load_and_list_newest_first(self, tmp_path):
        store = SessionStore(tmp_path / "s", str(tmp_path / "proj"))
        first, second = store.new_record(), store.new_record()
        first.turns = [turn("one")]
        second.turns = [turn("two")]
        store.save(first)
        store.save(second)
        assert [r.first_prompt for r in store.list()] == ["two", "one"]
        assert store.load(first.id).turns == first.turns and store.load("nope") is None

    def test_each_directory_has_its_own_sessions(self, tmp_path):
        a = SessionStore(tmp_path / "s", str(tmp_path / "a"))
        b = SessionStore(tmp_path / "s", str(tmp_path / "b"))
        record = a.new_record()
        record.turns = [turn()]
        a.save(record)
        assert len(a.list()) == 1 and b.list() == []
        assert directory_key(tmp_path / "a") != directory_key(tmp_path / "b")

    def test_only_the_most_recent_are_kept(self, tmp_path):
        store = SessionStore(tmp_path / "s", str(tmp_path / "p"), keep=2)
        records = []
        for number in range(4):
            record = store.new_record()
            record.id = f"r{number}"
            record.turns = [turn(f"q{number}")]
            records.append(record)
            dropped = store.save(record)
        assert [r.id for r in store.list()] == ["r3", "r2"]
        assert [r.id for r in dropped] == ["r1"]

    def test_unavailable_store_does_nothing(self, tmp_path):
        store = SessionStore(tmp_path / "s", None)
        record = store.new_record()
        assert not store.available and store.save(record) == [] and store.list() == [] and store.load("x") is None
        assert not (tmp_path / "s").exists()

    def test_damaged_files_are_skipped_and_ids_are_checked(self, tmp_path):
        store = SessionStore(tmp_path / "s", str(tmp_path / "p"))
        record = store.new_record()
        record.turns = [turn()]
        store.save(record)
        (store.folder / "bad.json").write_text("{not json")
        (store.folder / "alien.json").write_text(json.dumps({"no": "id"}))
        assert [r.id for r in store.list()] == [record.id]
        assert store.load("../x") is None  # not a file name

    def test_a_failing_disk_loses_history_but_never_raises(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        store = SessionStore(blocker / "sub", str(tmp_path / "p"))
        record = store.new_record()
        record.turns = [turn()]
        assert store.save(record) == []


# --------------------------------------------------------------------------- #
# Through a real console: quit, restart in the same folder, /restore
# --------------------------------------------------------------------------- #


def first_session(tmp_path, **options):
    """A session that was lived: tools turned off, two questions, a checklist."""
    session = Session(tmp_path, permission_level=2, memory=True, **options)
    session.console_.tool_context.todos.replace([{"content": "step one", "status": "in_progress"}])

    def driver(s):
        s.send("/tools off run_command")
        s.wait_output("Turned off")
        s.send("hello there")
        s.wait_output("Done: hello there")
        s.send("second question")
        s.wait_output("Done: second question")

    out = session.run(driver)
    return session, out


class TestRestore:
    def test_a_restart_starts_fresh_and_hints_at_the_previous_session(self, tmp_path):
        first, _ = first_session(tmp_path)
        second = Session(tmp_path, permission_level=2, memory=True)
        out = second.run(lambda s: None)
        assert "Previous session" in out and "hello there" in out and "2 exchange(s)" in out and "/restore" in out
        assert second.console_.record.turns == [] and second.console_.context.session_id != first.console_.context.session_id
        assert second.clara.bodies == []  # nothing of the old conversation was sent

    def test_restore_brings_back_screen_model_tools_level_and_checklist(self, tmp_path):
        first, _ = first_session(tmp_path)
        old_conversation = first.console_.context.session_id
        second = Session(tmp_path, permission_level=0, memory=True)
        second.clara.messages = 4  # the server remembers the conversation
        second.console_.toolset.reset()  # the saved default would hide run_command: only the session remembers it
        second.console_.apply_tools()
        second.console_.tool_context.reads.mark(str(tmp_path / "work" / "visible.txt"))

        def driver(s):
            s.send("/restore")
            s.wait_output("Restored:")

        out = second.run(driver)
        console = second.console_
        assert "hello there" in out and "Done: hello there" in out and "second question" in out  # the screen
        assert "Session of" in out and "2 exchange(s)" in out
        assert console.context.session_id == old_conversation and console.record.id == first.console_.record.id
        assert "run_command" not in [t.__name__ for t in console.toolset.enabled()]
        assert "no memory of this conversation" not in out
        assert console.tool_context.gate.auto_level == 2 and "auto-accept level 2" in out
        assert console.tool_context.todos.render() == "◐ step one"
        with pytest.raises(Exception):  # files read in the new session no longer count: the agent must read again
            console.tool_context.reads.check(str(tmp_path / "work" / "visible.txt"))
        # the saved default for new sessions was not touched by restoring
        assert json.loads(second.settings.agent_tools_path.read_text(encoding="utf-8")) == {"disabled": []}

    def test_the_restored_session_goes_on_where_it_stopped(self, tmp_path):
        first, _ = first_session(tmp_path)
        second = Session(tmp_path, permission_level=2, memory=True)

        def driver(s):
            s.send("/restore")
            s.wait_output("Restored:")
            s.send("third question")
            s.wait_output("Done: third question")

        second.run(driver)
        saved = second.console_.store.list()
        assert len(saved) == 1 and [t["prompt"] for t in saved[0].turns] == ["hello there", "second question", "third question"]

    def test_clear_starts_a_session_of_its_own(self, tmp_path):
        session = Session(tmp_path, memory=True)

        def driver(s):
            s.send("old topic")
            s.wait_output("Done: old topic")
            s.send("/clear")
            wait_for(lambda: s.console_.record.turns == [])
            wait_for(s.idle)
            s.send("new topic")
            s.wait_output("Done: new topic")
            s.send("/restore")
            s.wait_output("Restored:")

        out = session.run(driver)
        assert len(session.console_.store.list()) == 2
        assert "old topic" in out and session.console_.record.turns[0]["prompt"] == "old topic"

    def test_list_and_numbers(self, tmp_path):
        first_session(tmp_path)
        second = Session(tmp_path, memory=True)

        def driver(s):
            s.send("later question")
            s.wait_output("Done: later question")
            s.send("/restore list")
            s.wait_output("Sessions of")
            s.send("/restore 1")
            s.wait_output("the session you are in")
            s.send("/restore 2")
            s.wait_output("Restored:")
            s.send("/restore 7")
            s.wait_output("Usage: /restore")

        out = second.run(driver)
        assert "hello there" in out and "later question" in out and "current" in out

    def test_nothing_to_restore(self, tmp_path):
        session = Session(tmp_path, memory=True)

        def driver(s):
            s.send("/restore")
            s.wait_output("No previous session")
            s.send("/restore list")
            s.wait_output("No saved session")

        session.run(driver)

    def test_without_memory_nothing_is_saved_and_restore_says_why(self, tmp_path):
        session = Session(tmp_path, memory=False)

        def driver(s):
            s.send("hello")
            s.wait_output("Done: hello")
            s.send("/restore")
            s.wait_output("--no-memory")

        session.run(driver)
        assert not session.settings.agent_sessions_dir.exists()

    def test_a_session_that_never_got_a_question_is_not_saved(self, tmp_path):
        session = Session(tmp_path, memory=True)
        session.run(lambda s: None)
        assert not session.settings.agent_sessions_dir.exists()

    def test_the_server_forgetting_the_conversation_is_pointed_out(self, tmp_path):
        first_session(tmp_path)
        second = Session(tmp_path, memory=True)
        second.clara.messages = 0  # the server has nothing left of it

        def driver(s):
            s.send("/restore")
            s.wait_output("Clara will not remember it")

        second.run(driver)

    def test_an_unreachable_server_is_pointed_out_too(self, tmp_path):
        first_session(tmp_path)
        second = Session(tmp_path, memory=True)

        def driver(s):
            s.clara.down = True
            s.send("/restore")
            s.wait_output("could not be asked")

        second.run(driver)

    def test_long_sessions_show_only_the_latest_exchanges(self, tmp_path):
        session = Session(tmp_path, memory=True)
        record = session.console_.store.new_record()
        record.turns = [turn(f"question {n}", f"answer {n}") for n in range(45)]
        record.model = "gemma4:test"
        record.context = {"conversation": "console_session-x", "summary": ""}
        session.console_.store.save(record)

        def driver(s):
            s.send("/restore")
            s.wait_output("Restored:")

        out = session.run(driver)
        assert "5 earlier exchange(s) not shown" in out and "question 44" in out and "question 4\n" not in out

    def test_arguments_are_completed(self, tmp_path):
        from prompt_toolkit.document import Document

        from custom_console.agent.slash import SlashCompleter

        first_session(tmp_path)
        second = Session(tmp_path, memory=True)
        completer = SlashCompleter(second.console_.screen.commands)
        found = {c.text: c.display_meta_text for c in completer.get_completions(Document("/restore "), None)}
        second.pipe_ctx.__exit__(None, None, None)
        assert set(found) == {"list", "1"} and "hello there" in found["1"]

    def test_old_sessions_beyond_the_limit_are_dropped(self, tmp_path):
        session = Session(tmp_path, memory=True, AGENT_KEEP_SESSIONS="2")
        conversations = []

        def driver(s):
            for number in range(3):
                conversations.append(s.console_.context.session_id)
                s.send(f"topic {number}")
                s.wait_output(f"Done: topic {number}")
                s.send("/clear")
                wait_for(lambda: s.console_.record.turns == [])
                wait_for(s.idle)

        session.run(driver)
        kept = session.console_.store.list()
        assert [r.first_prompt for r in kept] == ["topic 2", "topic 1"]
        assert session.clara.forgotten == [conversations[0]]  # the dropped session is erased from the server too
