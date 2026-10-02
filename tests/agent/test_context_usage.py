from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput

from custom_console.agent.context import (
    DEFAULT_WINDOW,
    LOCAL_WINDOW_CAP,
    MESSAGE_CHARS,
    ContextManager,
    build_transcript,
    choose_window,
    context_tokens_of,
    estimate_messages,
    estimate_tokens,
)
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.session import AgentSession
from custom_console.agent.turn import TurnStats, TurnView
from custom_console.agent.usage import UsageLedger, format_count, usage_tables
from custom_console.llm.ollama import ModelInfo


def msg(role, content="", **extra):
    return SimpleNamespace(role=role, content=content, tool_calls=extra.pop("tool_calls", None), **extra)


# --------------------------------------------------------------------------- #
# Window, estimates, transcript
# --------------------------------------------------------------------------- #


class TestWindow:
    def test_local_models_get_a_capped_window_that_is_requested(self):
        big = ModelInfo("m", context_length=262_144)
        assert choose_window(big, None) == (LOCAL_WINDOW_CAP, LOCAL_WINDOW_CAP)

    def test_small_models_keep_their_own_maximum(self):
        assert choose_window(ModelInfo("m", context_length=4096), None) == (4096, 4096)
        assert choose_window(ModelInfo("m", context_length=4096), 16_384) == (4096, 4096)  # never above the model's limit

    def test_the_user_can_ask_for_more(self):
        assert choose_window(ModelInfo("m", context_length=131_072), 65_536) == (65_536, 65_536)

    def test_remote_models_use_their_window_and_request_nothing(self):
        assert choose_window(ModelInfo("m", context_length=262_144, remote=True), 8000) == (262_144, None)

    def test_unknown_model(self):
        assert choose_window(None, None) == (DEFAULT_WINDOW, DEFAULT_WINDOW)


class TestEstimates:
    def test_estimate_tokens(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("x" * 35) == 10

    def test_system_messages_are_not_counted_as_conversation(self):
        messages = [msg("system", "x" * 700), msg("user", "x" * 35), msg("assistant", "y" * 35)]
        assert estimate_messages(messages) == 20

    def test_tool_calls_count(self):
        call = {"function": {"name": "f", "arguments": {"a": "b" * 100}}}
        assert estimate_messages([msg("assistant", "", tool_calls=[call])]) > 25

    def test_context_size_comes_from_the_last_assistant_message(self):
        early = msg("assistant", "a", metrics=SimpleNamespace(input_tokens=100, output_tokens=10))
        late = msg("assistant", "b", metrics=SimpleNamespace(input_tokens=500, output_tokens=20))
        assert context_tokens_of([msg("user"), early, msg("tool"), late]) == 520
        assert context_tokens_of([msg("user")]) is None
        assert context_tokens_of([msg("assistant", metrics=SimpleNamespace(input_tokens=0, output_tokens=0))]) is None


class TestTranscript:
    def test_roles_and_tool_activity(self):
        call = {"function": {"name": "file_system_read", "arguments": {}}}
        transcript = build_transcript(
            [
                msg("system", "ignored"),
                msg("user", "read a.txt"),
                msg("assistant", "", tool_calls=[call]),
                msg("tool", "x" * 1000, tool_name="file_system_read"),
                msg("assistant", "It says hello."),
            ]
        )
        lines = transcript.splitlines()
        assert lines[0] == "User: read a.txt"
        assert lines[1] == "Assistant: [called: file_system_read]"
        assert lines[2].startswith("[file_system_read result] xxx") and len(lines[2]) < 400
        assert lines[3] == "Assistant: It says hello." and "ignored" not in transcript

    def test_long_messages_are_cut(self):
        transcript = build_transcript([msg("user", "y" * 10_000)])
        assert len(transcript) < MESSAGE_CHARS + 50 and transcript.endswith("[…]")

    def test_oldest_messages_are_dropped_first_when_over_budget(self):
        messages = [msg("user", f"message number {i}") for i in range(100)]
        transcript = build_transcript(messages, budget=300)
        assert transcript.splitlines()[0] == "[earlier messages omitted]"
        assert transcript.splitlines()[-1] == "User: message number 99" and len(transcript) < 400


# --------------------------------------------------------------------------- #
# ContextManager
# --------------------------------------------------------------------------- #


class TestContextManager:
    def test_percent_uses_the_model_report_when_plausible(self):
        context = ContextManager(base_session_id="s", window=1000)
        context.set_static("x" * 350, tools_tokens=100, tool_count=3)  # 100 + 100 estimated
        context.observe(measured=400, messages_estimate=100)  # estimate 300
        assert context.breakdown().used == 400 and context.percent == 40.0

    def test_a_suspiciously_low_report_falls_back_to_the_estimate(self):
        context = ContextManager(base_session_id="s", window=1000)
        context.set_static("x" * 350, tools_tokens=300, tool_count=3)
        context.observe(measured=50, messages_estimate=300)  # cached prompt tokens are not counted by Ollama
        assert context.breakdown().used == 700

    def test_breakdown_parts(self, tmp_path):
        project = tmp_path / "AGENT.md"
        project.write_text("p" * 70)
        context = ContextManager(base_session_id="s", window=1000, project_file=project)
        context.set_static("x" * 35, tools_tokens=50, tool_count=2)
        context.adopt_summary("s" * 35)
        report = context.breakdown()
        assert (report.instructions, report.project, report.summary, report.tools) == (10, 20, 10, 50)
        assert report.tool_count == 2 and report.free == 1000 - report.used

    def test_auto_compaction_threshold(self):
        context = ContextManager(base_session_id="s", window=1000, compact_percent=80)
        context.observe(measured=790, messages_estimate=790)
        assert not context.should_compact()
        context.observe(measured=800, messages_estimate=800)
        assert context.should_compact()
        assert not ContextManager(base_session_id="s", window=10, compact_percent=0).should_compact()

    def test_project_file_is_read_fresh_and_limited(self, tmp_path):
        project = tmp_path / "AGENT.md"
        context = ContextManager(base_session_id="s", project_file=project)
        assert context.additional_context() == ""
        project.write_text("Use tabs.")
        assert "Use tabs." in context.additional_context() and "AGENT.md" in context.additional_context()
        project.write_text("x" * 50_000)
        assert len(context.project_instructions()) == 8000

    def test_summary_goes_into_the_additional_context(self):
        context = ContextManager(base_session_id="s")
        context.adopt_summary("  We built X.  ")
        assert "We built X." in context.additional_context() and "summary" in context.additional_context().lower()

    def test_compaction_and_clear_move_to_a_new_session(self):
        context = ContextManager(base_session_id="base")
        assert context.session_id == "base"
        context.adopt_summary("s")
        assert context.session_id == "base-1"
        context.reset()
        assert context.session_id == "base-2" and context.summary == ""

    def test_state_can_be_saved_and_restored(self):
        first = ContextManager(base_session_id="base")
        first.adopt_summary("remember this")
        state = first.state()
        assert state == {"base": "base", "generation": 1, "summary": "remember this"}
        second = ContextManager(base_session_id="elsewhere")
        second.restore(state)
        assert (second.base_session_id, second.generation, second.summary, second.session_id) == (
            "base", 1, "remember this", "base-1"
        )

    def test_restoring_tolerates_missing_fields(self):
        context = ContextManager(base_session_id="keep")
        context.restore({})
        assert (context.session_id, context.summary) == ("keep", "")

    def test_clear_can_start_a_session_apart_from_the_old_one(self):
        context = ContextManager(base_session_id="old")
        context.adopt_summary("s")
        context.reset("new")
        assert (context.session_id, context.summary) == ("new", "")


# --------------------------------------------------------------------------- #
# Session: usage, context observation, compaction
# --------------------------------------------------------------------------- #


class FakeAgent:
    def __init__(self, chunks, history=None):
        self.chunks, self.history = chunks, history or []
        self.additional_context = "unset"

    def run(self, prompt, **kwargs):
        self.kwargs = kwargs
        yield from self.chunks

    def get_chat_history(self, session_id=None, last_n_runs=None):
        self.history_session = session_id
        return self.history


def make_session(tmp_path, window=1000, **context_options):
    context = ContextManager(base_session_id="s", window=window, **context_options)
    usage = UsageLedger(tmp_path / "usage.jsonl")
    session = AgentSession(JsonlLogger(tmp_path / "log.jsonl"), "u", context, usage, model="m1")
    return session, context, usage


def run_output(tokens_in, tokens_out, last_call_in):
    final = msg("assistant", "done", metrics=SimpleNamespace(input_tokens=last_call_in, output_tokens=5))
    return RunOutput(
        content="done",
        metrics=RunMetrics(input_tokens=tokens_in, output_tokens=tokens_out, total_tokens=tokens_in + tokens_out),
        messages=[final],
    )


class TestSessionAccounting:
    def test_turn_records_usage_and_context(self, tmp_path):
        session, context, usage = make_session(tmp_path)
        session.agent = FakeAgent([SimpleNamespace(event="RunContent", content="hi"), run_output(900, 40, 300)])
        stats = session.run_turn(TurnView("q"), threading.Event())

        assert stats.context_percent == pytest.approx(context.percent)
        assert context.breakdown().used >= 305  # what the model saw at its last call
        totals = usage.summary().everything
        assert (totals.turns, totals.input_tokens, totals.output_tokens) == (1, 900, 40)
        assert usage.session_models["m1"].total_tokens == 940

    def test_the_agent_receives_the_current_additional_context_and_session(self, tmp_path):
        session, context, _ = make_session(tmp_path)
        context.adopt_summary("earlier work")
        session.agent = FakeAgent([])
        session.run_turn(TurnView("q"), threading.Event())
        assert "earlier work" in session.agent.additional_context
        assert session.agent.kwargs["session_id"] == "s-1"

    def test_no_additional_context_when_there_is_nothing_to_add(self, tmp_path):
        session, _, _ = make_session(tmp_path)
        session.agent = FakeAgent([])
        session.run_turn(TurnView("q"), threading.Event())
        assert session.agent.additional_context is None

    def test_a_full_context_compacts_automatically_after_the_turn(self, tmp_path):
        session, context, _ = make_session(tmp_path, window=500, compact_percent=80)
        history = [msg("user", "build it"), msg("assistant", "built")]
        session.agent = FakeAgent([run_output(800, 10, 450)], history)
        session.summarize = lambda transcript, previous, focus: "summary of the work"

        view = TurnView("q")
        session.run_turn(view, threading.Event())

        notes = [s.text for s in view.snapshot() if s.kind == "note"]
        assert any("compacting" in n for n in notes) and any("compacted" in n for n in notes)
        assert context.summary == "summary of the work" and context.session_id == "s-1"
        assert context.percent < 80

    def test_no_compaction_after_an_interrupted_turn(self, tmp_path):
        session, context, _ = make_session(tmp_path, window=500)
        session.agent = FakeAgent([run_output(800, 10, 450)])
        session.summarize = lambda *a: pytest.fail("must not compact")
        cancel = threading.Event()
        cancel.set()
        session.run_turn(TurnView("q"), cancel)
        assert context.summary == ""

    def test_a_failing_summariser_is_reported_not_raised(self, tmp_path):
        session, context, _ = make_session(tmp_path, window=500)
        session.agent = FakeAgent([run_output(800, 10, 450)], [msg("user", "x")])

        def broken(*args):
            raise RuntimeError("model unavailable")

        session.summarize = broken
        view = TurnView("q")
        session.run_turn(view, threading.Event())
        assert any("Could not compact" in s.text and "model unavailable" in s.text for s in view.snapshot())
        assert context.summary == ""

    def test_compact_passes_the_previous_summary_and_focus(self, tmp_path):
        session, context, _ = make_session(tmp_path)
        context.adopt_summary("first summary")
        session.agent = FakeAgent([], [msg("user", "more work")])
        seen = {}

        def summarize(transcript, previous, focus):
            seen.update(transcript=transcript, previous=previous, focus=focus)
            return "second summary"

        session.summarize = summarize
        session.compact("the tests")
        assert seen == {"transcript": "User: more work", "previous": "first summary", "focus": "the tests"}
        assert session.agent.history_session == "s-1"  # summarised the conversation that was running
        assert context.summary == "second summary" and context.session_id == "s-2"

    def test_compacting_an_empty_conversation(self, tmp_path):
        session, _, _ = make_session(tmp_path)
        session.agent = FakeAgent([], [])
        session.summarize = lambda *a: "x"
        with pytest.raises(LookupError):
            session.compact()

    def test_clear_resets_context_todos_and_reads(self, tmp_path):
        from custom_console.agent.tools.state import ReadTracker, TodoList

        session, context, _ = make_session(tmp_path)
        session.todos, session.reads = TodoList(), ReadTracker()
        session.todos.replace(["a"])
        session.reads.mark(str(tmp_path))
        context.adopt_summary("old")
        session.agent = FakeAgent([])
        session.clear()
        assert context.summary == "" and session.todos.items == [] and session.agent.additional_context is None
        with pytest.raises(PermissionError):
            session.reads.check(str(tmp_path))


# --------------------------------------------------------------------------- #
# Usage ledger
# --------------------------------------------------------------------------- #


class TestUsageLedger:
    def test_turns_are_persisted_and_summed_by_period(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        now = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)

        def write(days_ago, model, tokens_in, tokens_out):
            when = now - timedelta(days=days_ago)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": when.isoformat(), "model": model, "input": tokens_in, "output": tokens_out, "duration": 1}) + "\n")

        write(0.001, "a", 10, 5)
        write(3, "a", 100, 50)
        write(40, "b", 1000, 500)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("garbage line\n")

        ledger = UsageLedger(path)
        summary = ledger.summary(now=now)
        assert (summary.today.total_tokens, summary.week.total_tokens, summary.everything.total_tokens) == (15, 165, 1665)
        assert summary.everything.turns == 3
        assert summary.by_model["a"].total_tokens == 165 and summary.by_model["b"].total_tokens == 1500

    def test_recording_appends_and_tracks_the_session(self, tmp_path):
        ledger = UsageLedger(tmp_path / "sub" / "usage.jsonl")
        ledger.record("m", TurnStats(10, 5, 15, 2.0))
        ledger.record("m", TurnStats(20, 5, 25, 1.0))
        again = UsageLedger(tmp_path / "sub" / "usage.jsonl")  # a later session
        summary = again.summary()
        assert summary.everything.total_tokens == 40 and summary.session.turns == 0
        assert ledger.summary().session.total_tokens == 40 and ledger.session_models["m"].turns == 2

    def test_without_a_file_only_the_session_is_known(self):
        ledger = UsageLedger(None)
        ledger.record("m", TurnStats(1, 1, 2, 0.1))
        assert ledger.summary().session.turns == 1 and ledger.summary().everything.turns == 0

    def test_unwritable_ledger_does_not_break_recording(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        UsageLedger(blocker / "usage.jsonl").record("m", TurnStats(1, 1, 2, 0.1))

    @pytest.mark.parametrize("value, text", [(5, "5"), (999, "999"), (1000, "1k"), (12_345, "12.3k"), (2_500_000, "2.50M")])
    def test_format_count(self, value, text):
        assert format_count(value) == text

    def test_tables_list_models_seen_in_either_period(self, tmp_path):
        ledger = UsageLedger(tmp_path / "u.jsonl")
        ledger.record("m1", TurnStats(10, 5, 15, 2.0))
        tables = usage_tables(ledger.summary(), ledger.session_models)
        assert len(tables) == 2 and tables[1].row_count == 1 and tables[0].row_count == 4
