from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fake_clara import FakeClara, say

from custom_console.agent.clara import ClaraError, NothingToCompact
from custom_console.agent.context import ContextManager, estimate_tokens
from custom_console.agent.journal import JsonlLogger
from custom_console.agent.remote import RemoteAgent
from custom_console.agent.session import AgentSession
from custom_console.agent.turn import TurnStats, TurnView
from custom_console.agent.usage import UsageLedger, format_count, usage_tables


# --------------------------------------------------------------------------- #
# Estimates
# --------------------------------------------------------------------------- #


class TestEstimates:
    def test_estimate_tokens(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("x" * 35) == 10


# --------------------------------------------------------------------------- #
# ContextManager
# --------------------------------------------------------------------------- #


class TestContextManager:
    def test_the_servers_report_is_what_counts(self):
        context = ContextManager(base_session_id="s", window=1000)
        context.set_static("x" * 350, tools_tokens=100, tool_count=3)  # 100 + 100 estimated
        context.observe({"tokens": 400, "window": 2000, "percent": 20})
        report = context.breakdown()
        assert report.used == 400 and report.window == 2000 and context.percent == 20.0
        assert report.messages == 200  # what is neither the prompt nor the tools

    def test_before_any_report_the_fixed_parts_are_estimated(self):
        context = ContextManager(base_session_id="s", window=1000)
        context.set_static("x" * 350, tools_tokens=100, tool_count=3)
        assert context.breakdown().used == 200 and context.breakdown().messages == 0

    def test_an_empty_report_changes_nothing(self):
        context = ContextManager(base_session_id="s", window=1000)
        context.observe({"tokens": 300, "window": 1000})
        context.observe(None)
        context.observe({})
        assert context.breakdown().used == 300

    def test_breakdown_parts(self, tmp_path):
        project = tmp_path / "AGENT.md"
        project.write_text("p" * 70)
        context = ContextManager(base_session_id="s", window=1000, project_file=project)
        context.set_static("x" * 35, tools_tokens=50, tool_count=2)
        context.summary = "s" * 35
        report = context.breakdown()
        assert (report.instructions, report.project, report.summary, report.tools) == (10, 20, 10, 50)
        assert report.tool_count == 2 and report.free == 1000 - report.used

    def test_project_file_is_read_fresh_and_limited(self, tmp_path):
        project = tmp_path / "AGENT.md"
        context = ContextManager(base_session_id="s", project_file=project)
        assert context.additional_context() == ""
        project.write_text("Use tabs.")
        assert "Use tabs." in context.additional_context() and "AGENT.md" in context.additional_context()
        project.write_text("x" * 50_000)
        assert len(context.project_instructions()) == 8000

    def test_turned_off_tools_go_into_the_additional_context(self):
        context = ContextManager(base_session_id="s")
        context.disabled_tools = ["run_command"]
        assert "Tools turned off by the user" in context.additional_context() and "run_command" in context.additional_context()

    def test_the_summary_is_the_servers_business(self):
        context = ContextManager(base_session_id="s")
        context.summary = "We built X."
        assert "We built X." not in context.additional_context()  # the server puts it in the prompt

    def test_clear_moves_to_a_conversation_of_its_own(self):
        context = ContextManager(base_session_id="old")
        context.summary = "s"
        context.observe({"tokens": 500})
        context.reset("new")
        assert (context.session_id, context.summary) == ("new", "")
        assert context.breakdown().used == 0

    def test_state_can_be_saved_and_restored(self):
        first = ContextManager(base_session_id="conv")
        first.summary = "remember this"
        state = first.state()
        assert state == {"conversation": "conv", "summary": "remember this"}
        second = ContextManager(base_session_id="elsewhere")
        second.restore(state)
        assert (second.session_id, second.summary) == ("conv", "remember this")

    def test_sessions_saved_by_older_versions_still_restore(self):
        context = ContextManager(base_session_id="elsewhere")
        context.restore({"base": "console_session-x", "generation": 2, "summary": "old"})
        assert context.session_id == "console_session-x"

    def test_restoring_tolerates_missing_fields(self):
        context = ContextManager(base_session_id="keep")
        context.restore({})
        assert (context.session_id, context.summary) == ("keep", "")


# --------------------------------------------------------------------------- #
# Session: usage, context, compaction
# --------------------------------------------------------------------------- #


def make_session(tmp_path, window=1000, **context_options):
    context = ContextManager(base_session_id="s", window=window, **context_options)
    usage = UsageLedger(tmp_path / "usage.jsonl")
    session = AgentSession(JsonlLogger(tmp_path / "log.jsonl"), "u", context, usage, model="m1")
    fake = FakeClara(window=window)
    session.remote = RemoteAgent(fake, lambda: [], lambda: "Base instructions.")
    return session, context, usage, fake


class TestSessionAccounting:
    def test_turn_records_usage_and_context(self, tmp_path):
        session, context, usage, fake = make_session(tmp_path)
        fake.respond = lambda body: say("hi", prompt=900, completion=40, tokens=300, window=1000, model="m1")
        stats = session.run_turn(TurnView("q"), threading.Event())

        assert stats.context_percent == pytest.approx(30.0) and context.percent == pytest.approx(30.0)
        assert context.breakdown().used == 300  # what the model saw at its last call
        totals = usage.summary().everything
        assert (totals.turns, totals.input_tokens, totals.output_tokens) == (1, 900, 40)
        assert usage.session_models["m1"].total_tokens == 940

    def test_the_servers_window_replaces_the_default(self, tmp_path):
        session, context, _, fake = make_session(tmp_path, window=1000)
        fake.respond = lambda body: say("hi", tokens=100, window=262_144)
        session.run_turn(TurnView("q"), threading.Event())
        assert context.window == 262_144

    def test_model_changes_made_on_the_server_are_followed(self, tmp_path):
        session, _, usage, fake = make_session(tmp_path)
        seen = []
        session.on_model = lambda model, provider: seen.append((model, provider))
        fake.respond = lambda body: say("one", model="m1", provider="")
        session.run_turn(TurnView("q"), threading.Event())
        assert seen == []  # the same model: nothing to announce

        fake.respond = lambda body: say("two", model="m2", provider="cloud")
        session.run_turn(TurnView("q"), threading.Event())
        assert seen == [("m2", "cloud")] and session.model == "m2"
        assert usage.session_models["m2"].turns == 1  # the turn is accounted to the model that ran it

    def test_the_request_carries_the_conversation_and_the_current_instructions(self, tmp_path):
        project = tmp_path / "AGENT.md"
        project.write_text("Use tabs.")
        session, context, _, fake = make_session(tmp_path, project_file=project)
        context.disabled_tools = ["run_command"]
        session.run_turn(TurnView("q"), threading.Event())
        [body] = fake.bodies
        assert body["conversation"] == "s"
        assert body["instructions"].startswith("Base instructions.\n\n## Project instructions (AGENT.md)\nUse tabs.")
        assert "run_command" in body["instructions"]

    def test_only_the_base_instructions_when_there_is_nothing_to_add(self, tmp_path):
        session, _, _, fake = make_session(tmp_path)
        session.run_turn(TurnView("q"), threading.Event())
        assert fake.bodies[0]["instructions"] == "Base instructions."

    def test_an_interrupted_turn_has_no_stats(self, tmp_path):
        session, _, usage, fake = make_session(tmp_path)
        cancel = threading.Event()
        cancel.set()
        assert session.run_turn(TurnView("q"), cancel) is None
        assert usage.summary().everything.turns == 0

    def test_compact_goes_to_the_server_and_refreshes_the_context(self, tmp_path):
        session, context, _, fake = make_session(tmp_path)
        fake.tokens = 120
        before, after = session.compact("the tests")
        assert (before, after) == (60.0, 10.0)
        assert fake.compactions == [("s", "the tests")]
        assert context.summary == fake.summary and context.breakdown().used == 120

    def test_compacting_an_empty_conversation(self, tmp_path):
        session, _, _, fake = make_session(tmp_path)
        fake.nothing_to_compact = True
        with pytest.raises(LookupError):
            session.compact()
        assert issubclass(NothingToCompact, ClaraError)

    def test_refreshing_without_a_server_gives_nothing(self, tmp_path):
        session, context, _, fake = make_session(tmp_path)
        fake.tokens = 500
        assert session.refresh_context()["tokens"] == 500 and context.breakdown().used == 500
        fake.down = True
        assert session.refresh_context() is None

    def test_clear_resets_context_todos_and_reads(self, tmp_path):
        from custom_console.agent.tools.state import ReadTracker, TodoList

        session, context, _, _ = make_session(tmp_path)
        session.todos, session.reads = TodoList(), ReadTracker()
        session.todos.replace(["a"])
        session.reads.mark(str(tmp_path))
        context.summary = "old"
        session.clear("fresh")
        assert context.summary == "" and context.session_id == "fresh" and session.todos.items == []
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
