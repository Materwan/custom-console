"""`local_task`: a separate incognito conversation with read-only tools, limits, and what it must not pass on."""

from __future__ import annotations

import asyncio
import sys

import pytest

from custom_console.core.subagent import RESULT_LIMIT, SubagentRunner
from custom_console.core.tools.base import ToolError
from tests.fake_clara.server import Hang, HttpError, WaitResults
from tests.support.harness import make_world

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
MAIN, SUB = "c0123456789abcdef", "c0fedcba987654321"


def turn(turn_id, conversation=MAIN, **extra):
    return {"type": "turn", "id": turn_id, "conversation": conversation, "model": "fake:small", "incognito": False,
            "local_only": False, **extra}


def done(reply="ok", conversation=MAIN, **extra):
    return {"type": "done", "reply": reply, "conversation": conversation, "tools": [], "message_id": 7, "versions": [7],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "context": {"tokens": 1, "window": 1000, "percent": 0.1},
            "model": "fake:small", "tainted": False, **extra}


def requests(turn_id, *calls):
    return {"type": "tool_requests", "turn": turn_id,
            "calls": [{"id": f"call_0_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]}


class View:
    def __init__(self):
        self.tools: list[tuple[int, str, dict]] = []
        self.events: list[tuple[int, object]] = []
        self.waiting: list[bool] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, bool, str]] = []

    def sub_started(self, label, prompt, round_limit):
        self.started.append(label)
        return len(self.started)

    def sub_event(self, number, event):
        self.events.append((number, event))

    def sub_tool(self, number, call, ok, result, seconds, preview):
        self.tools.append((number, call.name, call.arguments))

    def sub_waiting(self, number, waiting):
        self.waiting.append(waiting)

    def sub_finished(self, number, ok, summary):
        self.finished.append((number, ok, summary))


@pytest.fixture
def w(tmp_path):
    return make_world(tmp_path)


def attach(w, client, **limits):
    view = View()
    runner = w.runner(client)
    w.ctx.subagent = SubagentRunner(client=client, parent=w.session, registry=w.registry, engine=w.engine, ctx=w.ctx,
                                    ui=w.ui, view=view, journal=w.journal, ledger=w.ledger,
                                    parent_runner=lambda: runner, **limits)
    return runner, view


def main_turn(fake, *sub_steps, prompt="find the answer"):
    fake.queue_turn(turn("t1"), requests("t1", ("local_task", {"prompt": prompt, "description": "look"})), WaitResults(),
                    {"type": "token", "text": "back"}, done("main done"))
    fake.queue_turn(*sub_steps)


async def test_a_subagent_is_a_separate_incognito_conversation_that_only_reads(w, client, fake_clara):
    (w.work / "a.txt").write_text("alpha\n")
    main_turn(fake_clara, turn("s1", SUB, incognito=True), requests("s1", ("local_read", {"path": "a.txt"})), WaitResults(),
              done("It says alpha.", SUB))
    runner, view = attach(w, client)
    result = await runner.run("go")
    assert result.outcome == "ok"
    main_body, sub_body = (r["body"] for r in fake_clara.requests_to("/v1/chat/stream"))
    assert sub_body["message"] == "find the answer" and sub_body["incognito"] is True and "conversation" not in sub_body
    assert sub_body["parent"] == MAIN and "parent" not in main_body  # Clara shows it inside the main conversation
    offered = {t["function"]["name"] for t in sub_body["tools"]}
    readers = {"local_list", "local_read", "local_search", "local_git", "local_pdf_to_markdown"}
    assert {"local_list", "local_read", "local_search"} <= offered <= readers  # the PDF reader and git exist when installed
    assert "local_task" not in offered and "local_write" not in offered and "local_run" not in offered
    assert sub_body["instructions"].startswith("# The console: sub-agent")
    assert "local_task" in {t["function"]["name"] for t in main_body["tools"]}
    assert fake_clara.results["t1"]["call_0_0"] == "It says alpha."
    assert view.started == ["find the answer"] and view.finished == [(1, True, "1 tool calls")]
    assert view.tools == [(1, "local_read", {"path": "a.txt"})]
    assert {type(e).__name__ for _, e in view.events} >= {"TurnStarted", "ToolRequests", "Done"}  # what it does is shown
    assert w.session.conversation == MAIN and w.store.list()[0].conversation == MAIN  # the sub-agent never became the session


async def test_what_the_subagent_read_does_not_unlock_an_overwrite_for_the_main_agent(w, client, fake_clara):
    (w.work / "a.txt").write_text("alpha\n")
    main_turn(fake_clara, turn("s1", SUB), requests("s1", ("local_read", {"path": "a.txt"})), WaitResults(), done("read", SUB))
    runner, _ = attach(w, client)
    await runner.run("go")
    w.ui.answers = ["allow_once"]
    answer = await w.call("local_write", path="a.txt", content="overwritten")
    assert "read" in answer.lower() and (w.work / "a.txt").read_text() == "alpha\n"


async def test_outside_content_read_by_the_subagent_taints_the_main_turn(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), {"type": "tool_start", "name": "web_fetch", "arguments": {}}, done("web said x", SUB))
    runner, _ = attach(w, client)
    await runner.run("go")
    assert runner.tainted is True


async def test_a_clean_subagent_leaves_the_main_turn_clean(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), done("fine", SUB))
    runner, _ = attach(w, client)
    await runner.run("go")
    assert runner.tainted is False


async def test_a_prompt_of_the_subagent_goes_to_the_person_marked_as_its_own(w, client, fake_clara):
    outside = w.home / "elsewhere.txt"
    outside.write_text("x")
    main_turn(fake_clara, turn("s1", SUB), requests("s1", ("local_read", {"path": str(outside)})), WaitResults(), done("ok", SUB))
    w.ui.answers = ["refuse_once"]
    runner, view = attach(w, client)
    await runner.run("go")
    assert [p.summary.startswith("[sub-agent]") for p in w.ui.prompts] == [True]
    assert view.waiting == [True, False]  # the main tab shows that it waited for the person
    assert "refused" in fake_clara.results["s1"]["call_0_0"].lower()


async def test_the_round_limit_stops_the_subagent(w, client, fake_clara):
    call = ("local_list", {})
    main_turn(fake_clara, turn("s1", SUB), requests("s1", call), WaitResults(), requests("s1", call), WaitResults(),
              requests("s1", call), WaitResults(), done("never", SUB))
    runner, view = attach(w, client, round_limit=1)
    await runner.run("go")
    assert "round limit (1)" in fake_clara.results["t1"]["call_0_0"]
    assert view.finished[0][1] is False


async def test_the_person_stops_one_subagent_and_the_main_agent_goes_on(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), Hang())
    runner, view = attach(w, client)
    task = asyncio.create_task(runner.run("go"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if view.started:
            break
    await asyncio.sleep(0.2)
    assert w.ctx.subagent.stop(1) is True
    result = await task
    assert w.ctx.subagent.stop(1) is False  # nothing left to stop
    assert result.outcome == "ok"  # the main turn was not cancelled
    assert "stopped at the person's request" in fake_clara.results["t1"]["call_0_0"]
    assert view.finished[0][:2] == (1, False) and "person's request" in view.finished[0][2]


async def test_the_time_limit_stops_the_subagent(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), Hang())
    runner, view = attach(w, client, time_limit=0.4)
    await runner.run("go")
    assert "time limit" in fake_clara.results["t1"]["call_0_0"] and view.finished[0][:2] == (1, False)
    await asyncio.sleep(0.2)
    assert fake_clara.disconnects >= 1  # its connection was closed


async def test_cancelling_the_main_turn_stops_the_subagent_too(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), Hang())
    runner, view = attach(w, client)
    task = asyncio.create_task(runner.run("go"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if view.started:
            break
    await asyncio.sleep(0.2)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert view.finished and view.finished[0][1] is False and fake_clara.disconnects >= 2


async def test_a_failing_subagent_gives_a_sentence_not_an_exception(w, client, fake_clara):
    fake_clara.queue_turn(turn("t1"), requests("t1", ("local_task", {"prompt": "x"})), WaitResults(), done("end"))
    fake_clara.queue_turn(HttpError(502, "model_failed", "The model failed."))
    runner, view = attach(w, client)
    await runner.run("go")
    assert fake_clara.results["t1"]["call_0_0"] == "The sub-agent failed: The model failed."
    assert view.finished[0][:2] == (1, False)


async def test_a_long_answer_is_cut_and_an_empty_one_is_said(w, client, fake_clara):
    main_turn(fake_clara, turn("s1", SUB), done("x" * (RESULT_LIMIT * 3), SUB))
    runner, _ = attach(w, client)
    await runner.run("go")
    posted = fake_clara.results["t1"]["call_0_0"]
    assert len(posted) < RESULT_LIMIT + 200 and "characters left out" in posted
    main_turn(fake_clara, turn("s2", SUB), done("   ", SUB))
    fake_clara.results.clear()
    runner, _ = attach(w, client)
    fake_clara.turns.clear()
    fake_clara.queue_turn(turn("t2"), requests("t2", ("local_task", {"prompt": "x"})), WaitResults(), done("end"))
    fake_clara.queue_turn(turn("s2", SUB), done("   ", SUB))
    await runner.run("again")
    assert "gave no answer" in fake_clara.results["t2"]["call_0_0"]


async def test_without_a_hook_local_task_says_so(w, client, fake_clara):
    assert w.ctx.subagent is None
    with pytest.raises(ToolError, match="cannot start another sub-agent"):
        await w.run("local_task", prompt="x")


async def test_the_tokens_of_a_subagent_are_counted_under_its_own_label(w, client, fake_clara):
    """BUG-11: sub-agent tokens were missing from the statistics."""
    usage = {"type": "usage", "prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 0}
    main_turn(fake_clara, turn("s1", SUB), usage, done("fine", SUB))
    runner, _ = attach(w, client)
    await runner.run("go")
    totals = w.ledger.totals()
    assert totals.by_agent["sub"] == 120
    assert totals.by_agent.get("main", 0) == 0
