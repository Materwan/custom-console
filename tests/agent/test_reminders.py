"""Reminders in the console: reading /remind, wording the notice, and the background listener."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fake_clara import FakeClara

from custom_console.agent.reminders import ReminderListener, notice, parse_remind, take_targets

PARIS = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=PARIS)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


class TestParse:
    def test_relative_times(self):
        assert parse_remind("+30m Tea", NOW) == (NOW + timedelta(minutes=30), "", "Tea")
        assert parse_remind("+2h Call mum", NOW)[0] == NOW + timedelta(hours=2)
        assert parse_remind("+3d Rent", NOW)[0] == NOW + timedelta(days=3)

    def test_a_time_of_day_is_today_if_ahead_and_tomorrow_if_passed(self):
        assert parse_remind("18:30 Dinner", NOW)[0] == datetime(2026, 10, 2, 18, 30, tzinfo=PARIS)
        assert parse_remind("09:00 Stand-up", NOW)[0] == datetime(2026, 10, 3, 9, 0, tzinfo=PARIS)

    def test_tomorrow_and_a_full_date(self):
        assert parse_remind("tomorrow 9:05 Dentist", NOW)[0] == datetime(2026, 10, 3, 9, 5, tzinfo=PARIS)
        due, _, text = parse_remind("2026-12-24 20:00 Gifts", NOW)
        assert (due.year, due.month, due.day, due.hour, due.minute) == (2026, 12, 24, 20, 0)
        assert due.tzinfo is not None and text == "Gifts"

    def test_a_repeat_comes_first(self):
        assert parse_remind("weekly 09:00 Bins out", NOW) == (datetime(2026, 10, 3, 9, 0, tzinfo=PARIS), "weekly", "Bins out")

    @pytest.mark.parametrize(
        ("argument", "message"),
        [
            ("", "Usage"),
            ("daily", "Usage"),
            ("soon Tea", "Cannot read the time"),
            ("25:00 Tea", "Not a time of day"),
            ("2026-02-30 10:00 Tea", "Not a date"),
            ("+30m", "needs a text"),
            ("tomorrow", "Cannot read the time"),
        ],
    )
    def test_bad_input_is_explained(self, argument, message):
        with pytest.raises(ValueError, match=message):
            parse_remind(argument, NOW)


class TestNotice:
    def event(self, **fields):
        return {
            "type": "reminder", "id": 1, "text": "Dentist", "from": "Erwan",
            "due_at": "2026-10-02T10:00:00+00:00", "fired_at": "2026-10-02T10:00:00+00:00", **fields,
        }

    def test_a_reminder_that_just_fired(self):
        text = notice(self.event(), datetime(2026, 10, 2, 10, 0, 5, tzinfo=timezone.utc))
        assert "Dentist" in text and "missed" not in text

    def test_a_reminder_that_fired_while_the_console_was_closed(self):
        assert "missed, it was due" in notice(self.event(), datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc))

    def test_a_reminder_is_always_ones_own_so_its_author_is_not_shown(self):
        assert "Erwan" not in notice(self.event(), datetime(2026, 10, 2, 10, 0, 1, tzinfo=timezone.utc))

    def test_a_notification(self):
        event = {"type": "notification", "title": "Answer ready", "text": "Done.", "sent_at": "2026-10-02T10:00:00+00:00"}
        assert notice(event, datetime(2026, 10, 2, 10, 0, 1, tzinfo=timezone.utc)) == "🔔 Answer ready: Done."
        assert "(sent " in notice({**event, "title": ""}, datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc))

    def test_targets_are_an_at_word_before_the_time(self):
        assert take_targets("@App,discord +30m Tea") == (["app", "discord"], "+30m Tea")
        assert take_targets("weekly @app 09:00 Bins") == (["app"], "weekly 09:00 Bins")
        assert take_targets("+30m mail @bob") == ([], "+30m mail @bob")


    def test_the_message_clara_wrote_replaces_the_reminder_name(self):
        text = notice(self.event(message="Alice, your dentist is waiting!"), datetime(2026, 10, 2, 10, 0, 5, tzinfo=timezone.utc))
        assert "your dentist is waiting" in text and "Dentist" not in text


class TestListener:
    def listen(self, fake, pause=0.02):
        got = []
        listener = ReminderListener(fake, got.append, pause=pause)
        listener.start()
        return listener, got

    def test_every_announced_reminder_reaches_the_callback(self):
        fake = FakeClara()
        fake.announced = [{"type": "reminder", "text": "one"}, {"type": "reminder", "text": "two"}]
        listener, got = self.listen(fake)
        wait_for(lambda: len(got) == 2)
        listener.stop()
        assert [e["text"] for e in got] == ["one", "two"]

    def test_it_reconnects_and_gets_what_came_in_meanwhile(self):
        fake = FakeClara()
        listener, got = self.listen(fake)
        time.sleep(0.1)  # first connection: nothing, it ends
        fake.announced.append({"type": "reminder", "text": "later"})
        wait_for(lambda: got)
        listener.stop()
        assert got[0]["text"] == "later"

    def test_a_server_that_is_down_is_retried_quietly(self):
        fake = FakeClara()
        fake.down = True
        listener, got = self.listen(fake)
        time.sleep(0.15)  # several failed attempts, no crash
        fake.announced.append({"type": "reminder", "text": "back"})
        fake.down = False
        wait_for(lambda: got)
        listener.stop()
        assert got[0]["text"] == "back"

    def test_stop_ends_the_thread(self):
        fake = FakeClara()
        listener, _ = self.listen(fake, pause=30)  # would wait 30 s between attempts
        time.sleep(0.1)
        listener.stop()
        listener._thread.join(2)
        assert not listener._thread.is_alive()

    def test_the_callback_runs_on_the_listener_thread_not_the_callers(self):
        fake = FakeClara()
        fake.announced = [{"type": "reminder", "text": "x"}]
        seen = []
        listener = ReminderListener(fake, lambda event: seen.append(threading.current_thread().name), pause=0.02)
        listener.start()
        wait_for(lambda: seen)
        listener.stop()
        assert seen == ["reminders"]


def server(state):
    return {"type": "server", "state": state}


class TestServerStatus:
    def run(self, *connections):
        """Plays the connections in turn (each ends after its events); returns what the listener said."""
        fake = FakeClara()
        said, got = [], []
        listener = ReminderListener(fake, got.append, pause=0.02, on_status=said.append)
        scripts = list(connections)

        def play():
            return iter(scripts.pop(0) if scripts else [])

        fake.reminder_events = play
        listener.start()
        wait_for(lambda: not scripts)
        time.sleep(0.1)  # the last connection ends, and the listener notices
        listener.stop()
        return said, got

    def test_it_says_when_the_server_goes_away_and_when_it_is_back(self):
        said, got = self.run([server("running"), {"type": "reminder", "text": "x"}], [server("running")])
        assert said == ["Clara is not running.", "Clara is running again.", "Clara is not running."]
        assert [e["text"] for e in got] == ["x"]

    def test_it_says_when_the_server_is_stopping_and_then_gone_once(self):
        said, _ = self.run([server("running"), server("stopping"), server("stopped")])
        assert said == ["Clara is stopping: she finishes what is running and takes nothing new.", "Clara is not running."]

    def test_a_server_that_was_never_there_is_said_once(self):
        fake = FakeClara()
        fake.down = True
        said = []
        listener = ReminderListener(fake, lambda event: None, pause=0.02, on_status=said.append)
        listener.start()
        time.sleep(0.2)  # several attempts
        listener.stop()
        assert said == ["Clara is not running."]

    def test_nothing_is_said_without_a_callback(self):
        fake = FakeClara()
        fake.announced = [server("running")]
        listener = ReminderListener(fake, lambda event: None, pause=0.02)
        listener.start()
        time.sleep(0.1)
        listener.stop()
