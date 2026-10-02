"""What the agent is told with each message: the date and time, and the files that
changed since it read them."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

from fake_clara import FakeClara

from custom_console.agent.context import ContextManager, describe_now, turn_notes
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.remote import RemoteAgent
from custom_console.agent.session import AgentSession
from custom_console.agent.tools.state import ReadTracker, StaleFile
from custom_console.agent.turn import TurnView
from custom_console.agent.usage import UsageLedger

PARIS = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 1, 14, 32, 5, tzinfo=PARIS)


def touch(path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


class TestTime:
    def test_the_date_time_day_and_offset(self):
        assert describe_now(NOW) == "2026-10-01 14:32 (Thursday, UTC+02:00)"
        assert describe_now(datetime(2026, 1, 4, 9, 5, tzinfo=timezone(timedelta(hours=-5)))) == "2026-01-04 09:05 (Sunday, UTC-05:00)"

    def test_the_notes_without_changed_files(self):
        assert turn_notes(NOW) == (
            "[Automatic note, not written by the user. Current date and time: 2026-10-01 14:32 (Thursday, UTC+02:00).]"
        )


class TestStaleFiles:
    def test_unchanged_files_are_not_listed(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("one")
        tracker = ReadTracker()
        tracker.mark(str(target))
        assert tracker.stale() == []

    def test_a_file_changed_after_being_read(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("one")
        touch(target, NOW - timedelta(hours=1))
        tracker = ReadTracker()
        tracker.mark(str(target))
        target.write_text("one\ntwo")
        touch(target, NOW - timedelta(minutes=2))
        [stale] = tracker.stale()
        assert stale.path == os.path.abspath(target) and stale.complete
        assert abs(stale.modified - (NOW - timedelta(minutes=2)).timestamp()) < 1

    def test_reading_it_again_clears_the_notice(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("one")
        tracker = ReadTracker()
        tracker.mark(str(target))
        touch(target, NOW)
        assert tracker.stale()
        tracker.mark(str(target))
        assert tracker.stale() == []

    def test_partial_reads_are_followed_too(self, tmp_path):
        target = tmp_path / "big.py"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.saw_part(str(target))
        touch(target, NOW)
        assert tracker.stale() == [StaleFile(os.path.abspath(target), NOW.timestamp(), False)]

    def test_a_partial_read_does_not_allow_editing(self, tmp_path):
        import pytest

        target = tmp_path / "big.py"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.saw_part(str(target))
        with pytest.raises(PermissionError, match="not been read"):
            tracker.check(str(target))

    def test_a_partial_read_after_a_whole_one_keeps_it_whole(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.mark(str(target))
        tracker.saw_part(str(target))  # a truncated read
        tracker.mark(str(target), complete=False)  # an outline or a line range, untruncated
        touch(target, NOW)
        assert tracker.stale()[0].complete

    def test_a_partial_read_of_a_changed_file_is_partial(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.mark(str(target))
        touch(target, NOW)
        tracker.mark(str(target), complete=False)  # read again, but only a range of the new version
        touch(target, NOW + timedelta(minutes=1))
        assert not tracker.stale()[0].complete

    def test_a_deleted_file(self, tmp_path):
        target = tmp_path / "gone.txt"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.mark(str(target))
        target.unlink()
        assert tracker.stale()[0].modified is None

    def test_clear_forgets_the_reads(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.saw_part(str(target))
        tracker.clear()
        touch(target, NOW)
        assert tracker.stale() == []


class TestNotes:
    def test_changed_files_are_listed_with_when_and_how_much_was_read(self):
        stale = [
            StaleFile("C:/w/a.md", (NOW - timedelta(minutes=2)).timestamp(), True),
            StaleFile("C:/w/b.py", (NOW - timedelta(days=1)).timestamp(), False),
            StaleFile("C:/w/c.txt", None, True),
        ]
        notes = turn_notes(NOW, stale)
        assert "Files changed since you read them" in notes and "read them again" in notes
        assert "- C:/w/a.md (modified at 14:30)" in notes
        assert "- C:/w/b.py (modified on 2026-09-30 at 14:32, you had read only part of it)" in notes
        assert notes.endswith("- C:/w/c.txt (deleted)]")

    def test_a_long_list_is_cut(self):
        stale = [StaleFile(f"f{i}", NOW.timestamp(), True) for i in range(14)]
        notes = turn_notes(NOW, stale)
        assert "- f9 " in notes and "- f10 " not in notes and "… and 4 more" in notes


class TestSession:
    def session(self, tmp_path, reads):
        session = AgentSession(
            JsonlLogger(tmp_path / "log.jsonl"),
            "u",
            ContextManager(base_session_id="s"),
            UsageLedger(None),
            reads=reads,
            clock=lambda: NOW,
        )
        session.remote = RemoteAgent(FakeClara(), lambda: [], lambda: "Be useful.")
        return session

    def test_each_message_carries_the_time_and_the_changed_files_as_its_prefix(self, tmp_path):
        target = tmp_path / "notes.md"
        target.write_text("v1")
        reads = ReadTracker()
        reads.mark(str(target))
        session = self.session(tmp_path, reads)
        bodies = session.remote.client.bodies

        view = TurnView("what is in notes.md?")
        session.run_turn(view, threading.Event())
        first = bodies[0]
        assert first["message"] == "what is in notes.md?"  # the user's words, untouched
        assert first["prefix"].startswith("[Automatic note, not written by the user. Current date and time: 2026-10-01 14:32")
        assert "Files changed" not in first["prefix"]

        target.write_text("v2 with a new section")
        touch(target, NOW - timedelta(minutes=1))
        session.run_turn(TurnView("and now?"), threading.Event())
        second = bodies[1]
        assert f"- {os.path.abspath(target)} (modified at 14:31)" in second["prefix"]
        assert second["message"] == "and now?"

        reads.mark(str(target))  # the agent read it again
        session.run_turn(TurnView("thanks"), threading.Event())
        assert "Files changed" not in bodies[2]["prefix"]
        assert view.prompt == "what is in notes.md?"  # what the user sees and what is saved: unchanged

    def test_without_a_tracker_only_the_time(self, tmp_path):
        session = self.session(tmp_path, None)
        session.run_turn(TurnView("hi"), threading.Event())
        assert session.remote.client.bodies[0]["prefix"] == turn_notes(NOW)


class TestReadTool:
    def test_range_and_summary_reads_are_followed(self, make_ctx):
        from custom_console.agent.tools.filesystem import filesystem_tools

        ctx, _ = make_ctx()
        target = ctx.settings.project_root.parent / "files" / "code.py"
        target.write_text("def f():\n    return 1\n" * 3)
        read = next(tool for tool in filesystem_tools(ctx) if tool.__name__ == "file_system_read")
        assert read(str(target), mode="summary").success
        touch(target, NOW)
        [stale] = ctx.reads.stale()
        assert not stale.complete  # an outline is not the whole file

    def test_a_whole_read_is_complete(self, make_ctx):
        from custom_console.agent.tools.filesystem import filesystem_tools

        ctx, _ = make_ctx()
        target = ctx.settings.project_root.parent / "files" / "a.txt"
        target.write_text("hello")
        read = next(tool for tool in filesystem_tools(ctx) if tool.__name__ == "file_system_read")
        assert read(str(target)).success
        touch(target, NOW)
        assert ctx.reads.stale()[0].complete
