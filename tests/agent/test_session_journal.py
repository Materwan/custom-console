from __future__ import annotations

import json
import threading
import time

import pytest
from fake_clara import FakeClara, say

from custom_console.agent.cache import JsonCache
from custom_console.agent.context import ContextManager
from custom_console.agent.usage import UsageLedger
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.remote import RemoteAgent
from custom_console.agent.results import ToolResult
from custom_console.agent.session import AgentSession
from custom_console.agent.turn import TurnView


def read_entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


class TestJsonlLogger:
    def test_entries_are_timestamped_json_lines(self, tmp_path):
        log = JsonlLogger(tmp_path / "logs" / "agent.jsonl")
        log.log_prompt("héllo")
        log.log_answer("réponse")
        log.log_error("oops")
        log.log_tool_call("file_system_list", {"path": "."}, {"success": True}, 0.123456, error=None)

        entries = read_entries(log.path)
        assert [e["type"] for e in entries] == ["prompt", "answer", "error", "tool_call"]
        assert entries[0]["prompt"] == "héllo" and "timestamp" in entries[0]
        assert entries[3]["duration_seconds"] == 0.1235 and entries[3]["arguments"] == {"path": "."}

    def test_unserializable_values_do_not_break_logging(self, tmp_path):
        log = JsonlLogger(tmp_path / "a.jsonl")
        log.log_tool_call("t", {"obj": object()}, object(), 0.0)
        assert len(read_entries(log.path)) == 1

    def test_rotation(self, tmp_path):
        log = JsonlLogger(tmp_path / "a.jsonl", max_bytes=200)
        for index in range(20):
            log.log_prompt("x" * 50 + str(index))
        assert (tmp_path / "a.jsonl.1").exists()
        assert log.path.stat().st_size < 400
        assert all(entry["type"] == "prompt" for entry in read_entries(log.path))

    def test_logging_never_raises(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        JsonlLogger(blocker / "sub" / "a.jsonl").log_prompt("x")  # parent is a file


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


class TestJsonCache:
    def test_roundtrip(self, tmp_path):
        cache = JsonCache(tmp_path / "sub" / "cache.json")
        assert cache.get("k") is None
        cache.set("k", {"a": [1, 2]})
        assert cache.get("k") == {"a": [1, 2]}

    def test_ttl(self, tmp_path, monkeypatch):
        cache = JsonCache(tmp_path / "c.json")
        cache.set("k", 1)
        assert cache.get("k", max_age=60) == 1
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 120)
        assert cache.get("k", max_age=60) is None
        assert cache.get("k") == 1  # no max_age: never expires

    def test_corrupt_and_empty_files_are_tolerated(self, tmp_path):
        path = tmp_path / "c.json"
        cache = JsonCache(path)
        path.write_text("")
        assert cache.get("k") is None
        path.write_text("{broken")
        assert cache.get("k") is None
        cache.set("k", 1)  # recovers
        assert cache.get("k") == 1

    def test_keys_are_independent_and_clear_works(self, tmp_path):
        cache = JsonCache(tmp_path / "c.json")
        cache.set("a", 1)
        cache.set("b", 2)
        assert (cache.get("a"), cache.get("b")) == (1, 2)
        cache.clear()
        assert cache.get("a") is None
        cache.clear()  # idempotent

    def test_falsy_values_are_cacheable(self, tmp_path):
        cache = JsonCache(tmp_path / "c.json")
        cache.set("empty", [])
        assert cache.get("empty") == []


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


@pytest.fixture
def session(tmp_path):
    session = AgentSession(
        JsonlLogger(tmp_path / "log.jsonl"),
        "user-1",
        ContextManager(base_session_id="session-1"),
        UsageLedger(tmp_path / "usage.jsonl"),
        model="test-model",
    )
    session.remote = RemoteAgent(FakeClara(), lambda: [], lambda: "Be useful.")
    return session


def play(session, *events):
    """Make the fake server answer the next turn with `events`."""
    session.remote.client.respond = lambda body: list(events)
    return session.remote.client


class TestRunTurn:
    def test_streams_text_and_returns_stats(self, session, tmp_path):
        fake = play(session, *say("Hello world", prompt=4, completion=6, tokens=10))
        view = TurnView("hi")

        stats = session.run_turn(view, threading.Event())

        assert view.answer_text() == "Hello world"
        assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (4, 6, 10)
        [body] = fake.bodies
        assert body["message"] == "hi" and body["conversation"] == "session-1" and body["user_id"] == "tester"
        assert "prefix" not in body  # nothing to note: no working directory, no file changed
        assert body["instructions"] == "Be useful."
        assert [e["type"] for e in read_entries(tmp_path / "log.jsonl")] == ["prompt", "answer"]

    def test_the_text_arrives_in_pieces(self, session):
        play(
            session,
            {"type": "token", "text": "Hello "},
            {"type": "token", "text": "world"},
            *say("", prompt=1, completion=2)[1:],
        )
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "Hello world"

    def test_unknown_events_are_ignored(self, session):
        play(session, {"type": "turn", "id": "t"}, {"type": "mystery"}, *say("real"))
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "real"

    def test_a_stream_that_ends_early_is_reported_not_raised(self, session):
        play(session, {"type": "token", "text": "partial"})  # no `done`
        view = TurnView("hi")
        assert session.run_turn(view, threading.Event()) is None
        assert view.answer_text() == "partial"
        assert any("closed the stream" in s.text and s.style == "error" for s in view.snapshot())

    def test_server_errors_are_reported_not_raised(self, session, tmp_path):
        play(session, {"type": "token", "text": "partial"}, {"type": "error", "message": "model crashed"})
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "partial"
        assert any("model crashed" in s.text and s.style == "error" for s in view.snapshot())
        assert "error" in [e["type"] for e in read_entries(tmp_path / "log.jsonl")]

    def test_an_unreachable_server_is_reported_not_raised(self, session):
        session.remote.client.down = True
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert any("Cannot reach the Clara server" in s.text and s.style == "error" for s in view.snapshot())

    def test_unexpected_exceptions_are_reported_not_raised(self, session):
        def broken(body):
            raise KeyError("oops")

        session.remote.client.respond = broken
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert any("KeyError" in s.text and s.style == "error" for s in view.snapshot())

    def test_cancel_stops_streaming_and_leaves_a_note(self, session):
        cancel = threading.Event()

        def events():
            yield {"type": "token", "text": "one "}
            cancel.set()
            yield {"type": "token", "text": "two "}
            yield {"type": "token", "text": "three "}

        session.remote.client.respond = lambda body: events()
        view = TurnView("hi")
        session.run_turn(view, cancel)

        assert view.answer_text() == "one "
        assert view.snapshot()[-1].text == "Interrupted by the user."

    def test_state_is_released_after_the_turn(self, session):
        play(session, *say("x"))
        session.run_turn(TurnView("hi"), threading.Event())
        session.record_permission("late", "accepted")  # must not raise without a current view


class TestToolHook:
    def hook_env(self, session, tmp_path):
        view, cancel = TurnView("hi"), threading.Event()
        session._view, session._cancel = view, cancel
        return view, cancel

    def test_success_is_shown_journaled_and_returned_as_text(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        output = session.tool_hook("file_system_list", lambda **kw: ToolResult.ok(["a", "b"]), {"path": "."})

        assert output == "a\nb"
        note = view.snapshot()[0]
        assert note.text.startswith("✔ file_system_list(path='.')")
        entry = read_entries(tmp_path / "log.jsonl")[0]
        assert entry["type"] == "tool_call" and entry["result"] == {"success": True, "data": ["a", "b"]}

    def test_failed_result_is_flagged(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        output = session.tool_hook("t", lambda **kw: ToolResult.fail(FileNotFoundError("x")), {})
        assert output == "Error: FileNotFoundError: x"
        assert view.snapshot()[0].text.startswith("✘ t()") and view.snapshot()[0].style == "error"
        assert read_entries(tmp_path / "log.jsonl")[0]["error"] == "x"

    def test_exceptions_become_failed_results(self, session, tmp_path):
        self.hook_env(session, tmp_path)

        def explode(**kwargs):
            raise RuntimeError("kaboom")

        assert session.tool_hook("t", explode, {"a": 1}) == "Error: RuntimeError: kaboom"

    def test_plain_return_values_are_wrapped(self, session, tmp_path):
        self.hook_env(session, tmp_path)
        assert session.tool_hook("t", lambda **kw: "text", {}) == "text"

    def test_tools_do_not_run_after_a_cancel(self, session, tmp_path):
        view, cancel = self.hook_env(session, tmp_path)
        cancel.set()
        ran = []
        output = session.tool_hook("t", lambda **kw: ran.append(1), {})
        assert ran == [] and output.startswith("Error: ") and "Interrupted" in output

    def test_works_without_a_current_view(self, session, tmp_path):
        assert session.tool_hook("t", lambda **kw: ToolResult.ok(1), {}) == "1"

    def test_permission_decisions_are_shown_in_the_turn(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        session.record_permission("Agent wants to x.", "auto-accepted")
        assert view.snapshot()[0].text == "? Agent wants to x. → auto-accepted"
