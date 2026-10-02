from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput

from custom_console.agent.cache import JsonCache
from custom_console.agent.context import ContextManager
from custom_console.agent.usage import UsageLedger
from custom_console.agent.journal import JsonlLogger
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


class FakeAgent:
    """Stands in for an agno Agent: `run` yields the configured chunks."""

    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.calls = chunks, error, []

    def run(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


def content(text, event="RunContent"):
    return SimpleNamespace(event=event, content=text)


@pytest.fixture
def session(tmp_path):
    return AgentSession(
        JsonlLogger(tmp_path / "log.jsonl"),
        "user-1",
        ContextManager(base_session_id="session-1"),
        UsageLedger(tmp_path / "usage.jsonl"),
        model="test-model",
    )


class TestRunTurn:
    def test_streams_text_and_returns_stats(self, session, tmp_path):
        final = RunOutput(content="Hello world", metrics=RunMetrics(input_tokens=4, output_tokens=6, total_tokens=10))
        session.agent = FakeAgent([content("Hello "), content("world"), final])
        view = TurnView("hi")

        stats = session.run_turn(view, threading.Event())

        assert view.answer_text() == "Hello world"
        assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (4, 6, 10)
        prompt, kwargs = session.agent.calls[0]
        assert prompt.startswith("[Automatic note, not written by the user. Current date and time: ")
        assert prompt.endswith(").]\n\nhi")
        assert kwargs["stream"] is True and kwargs["yield_run_output"] is True
        assert (kwargs["user_id"], kwargs["session_id"]) == ("user-1", "session-1")
        assert [e["type"] for e in read_entries(tmp_path / "log.jsonl")] == ["prompt", "answer"]

    def test_non_content_events_are_ignored(self, session):
        session.agent = FakeAgent(
            [content("real"), content("TOOL NOISE", event="ToolCallStarted"), content("!", event="RunContent")]
        )
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "real!"

    def test_chunks_without_string_content_are_ignored(self, session):
        session.agent = FakeAgent([SimpleNamespace(content=None), SimpleNamespace(content=42), SimpleNamespace(content="ok")])
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "ok"

    def test_no_metrics_means_no_stats(self, session):
        session.agent = FakeAgent([content("x")])
        assert session.run_turn(TurnView("hi"), threading.Event()) is None

    def test_agent_errors_are_reported_not_raised(self, session, tmp_path):
        session.agent = FakeAgent([content("partial")], error=RuntimeError("model crashed"))
        view = TurnView("hi")
        session.run_turn(view, threading.Event())
        assert view.answer_text() == "partial"
        assert any("model crashed" in s.text and s.style == "error" for s in view.snapshot())
        assert "error" in [e["type"] for e in read_entries(tmp_path / "log.jsonl")]

    def test_cancel_stops_streaming_and_leaves_a_note(self, session):
        cancel = threading.Event()

        def chunks():
            yield content("one ")
            cancel.set()
            yield content("two ")
            yield content("three ")

        session.agent = SimpleNamespace(run=lambda *a, **k: chunks())
        view = TurnView("hi")
        session.run_turn(view, cancel)

        assert view.answer_text() == "one "
        assert view.snapshot()[-1].text == "Interrupted by the user."

    def test_state_is_released_after_the_turn(self, session):
        session.agent = FakeAgent([content("x")])
        session.run_turn(TurnView("hi"), threading.Event())
        session.record_permission("late", "accepted")  # must not raise without a current view


class TestToolHook:
    def hook_env(self, session, tmp_path):
        view, cancel = TurnView("hi"), threading.Event()
        session._view, session._cancel = view, cancel
        return view, cancel

    def test_success_is_shown_journaled_and_returned_as_compact_json(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        output = session.tool_hook("file_system_list", lambda **kw: ToolResult.ok(["a", "b"]), {"path": "."})

        assert json.loads(output) == {"success": True, "data": ["a", "b"]}
        note = view.snapshot()[0]
        assert note.text.startswith("✔ file_system_list(path='.')")
        entry = read_entries(tmp_path / "log.jsonl")[0]
        assert entry["type"] == "tool_call" and entry["result"] == {"success": True, "data": ["a", "b"]}

    def test_failed_result_is_flagged(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        output = session.tool_hook("t", lambda **kw: ToolResult.fail(FileNotFoundError("x")), {})
        assert json.loads(output)["success"] is False
        assert view.snapshot()[0].text.startswith("✘ t()") and view.snapshot()[0].style == "error"
        assert read_entries(tmp_path / "log.jsonl")[0]["error"] == "x"

    def test_exceptions_become_failed_results(self, session, tmp_path):
        self.hook_env(session, tmp_path)

        def explode(**kwargs):
            raise RuntimeError("kaboom")

        output = json.loads(session.tool_hook("t", explode, {"a": 1}))
        assert output == {"success": False, "error": "RuntimeError: kaboom"}

    def test_plain_return_values_are_wrapped(self, session, tmp_path):
        self.hook_env(session, tmp_path)
        assert json.loads(session.tool_hook("t", lambda **kw: "text", {})) == {"success": True, "data": "text"}

    def test_tools_do_not_run_after_a_cancel(self, session, tmp_path):
        view, cancel = self.hook_env(session, tmp_path)
        cancel.set()
        ran = []
        output = json.loads(session.tool_hook("t", lambda **kw: ran.append(1), {}))
        assert ran == [] and output["success"] is False and "Interrupted" in output["error"]

    def test_works_without_a_current_view(self, session, tmp_path):
        assert json.loads(session.tool_hook("t", lambda **kw: ToolResult.ok(1), {}))["data"] == 1

    def test_permission_decisions_are_shown_in_the_turn(self, session, tmp_path):
        view, _ = self.hook_env(session, tmp_path)
        session.record_permission("Agent wants to x.", "auto-accepted")
        assert view.snapshot()[0].text == "? Agent wants to x. → auto-accepted"
