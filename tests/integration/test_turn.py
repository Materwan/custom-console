"""The turn runner against the fake Clara: events, local tool rounds, approvals, cancel, errors, the ledger."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from custom_console.clara import ClaraClient
from custom_console.clara.types import Attachment
from custom_console.core import turn as turn_module
from custom_console.core.turn import NO_ANSWER_TEXT, REFUSED_TEXT
from tests.fake_clara.server import Hang, HttpError, Sleep, WaitDecision, WaitResults
from tests.support.harness import make_world

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
CID = "c0123456789abcdef"


def turn(turn_id="t1", conversation=CID, **extra):
    return {"type": "turn", "id": turn_id, "conversation": conversation, "model": "fake:small", "incognito": False,
            "local_only": False, **extra}


def done(reply="ok", conversation=CID, **extra):
    return {"type": "done", "reply": reply, "conversation": conversation, "tools": [], "message_id": 7, "versions": [7],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "context": {"tokens": 100, "window": 1000, "percent": 10.0},
            "model": "fake:small", "tainted": False, **extra}


def usage(p=10, c=3):
    return {"type": "usage", "prompt_tokens": p, "completion_tokens": c, "cached_tokens": 1}


def requests(turn_id, *calls):
    return {"type": "tool_requests", "turn": turn_id,
            "calls": [{"id": f"call_0_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]}


@pytest.fixture
def w(tmp_path):
    return make_world(tmp_path)


async def test_a_plain_turn_updates_the_session_the_ledger_and_the_journal(w, client, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "token", "text": "Hel"}, {"type": "token", "text": "lo"}, usage(), done("Hello"))
    result = await w.runner(client).run("hi")
    assert result.outcome == "ok" and result.reply == "Hello" and result.done is not None
    s = w.session
    assert (s.conversation, s.last_message_id, s.context_percent, s.prompt_tokens, s.completion_tokens) == (CID, 7, 10.0, 10, 3)
    totals = w.ledger.window("session", s.id)
    assert (totals.prompt, totals.completion, totals.cached, totals.turns) == (10, 3, 1, 1)
    assert [e["kind"] for e in w.journal.read()] == ["turn"]
    assert w.store.list()[0].conversation == CID  # the session file was written
    body = fake_clara.last("/v1/chat/stream")["body"]
    assert body["message"] == "hi" and body["instructions"] == "BASE PROMPT" and len(body["tools"]) == len(w.registry.offered(w.ctx))
    assert "conversation" not in body
    assert [type(e).__name__ for e in w.ui.events][:2] == ["TurnStarted", "Token"]


async def test_the_second_turn_continues_the_conversation_and_sends_no_creation_flags(w, client, fake_clara):
    w.session.incognito, w.session.local_only, w.session.project = True, True, 3
    fake_clara.queue_turn(turn(incognito=True, local_only=True), done())
    fake_clara.queue_turn(turn("t2", incognito=True, local_only=True), done())
    runner = w.runner(client)
    await runner.run("first")
    first = fake_clara.requests_to("/v1/chat/stream")[0]["body"]
    assert first["incognito"] is True and first["local_only"] is True and first["project"] == 3
    await runner.run("second")
    second = fake_clara.requests_to("/v1/chat/stream")[1]["body"]
    assert second["conversation"] == CID
    assert not {"incognito", "local_only", "project"} & set(second)
    assert w.store.list() == []  # an incognito session is never saved locally


async def test_a_local_tool_round_trip_posts_all_results_together(w, client, fake_clara):
    (w.work / "a.txt").write_text("alpha\n")
    fake_clara.queue_turn(turn(), requests("t1", ("local_read", {"path": "a.txt"}), ("local_list", {})), WaitResults(),
                          {"type": "token", "text": "done"}, done())
    result = await w.runner(client).run("look")
    assert result.outcome == "ok" and result.tools_run == 2 and result.tool_names == ["local_read", "local_list"]
    posted = fake_clara.results["t1"]
    assert posted["call_0_0"] == "1\talpha" and "a.txt" in posted["call_0_1"]
    assert len(fake_clara.requests_to("/v1/turns/t1/tool-results")) == 1
    assert w.ui.prompts == []  # reads inside the trusted folder never ask
    tools = [e for e in w.journal.read() if e["kind"] == "tool"]
    assert [t["name"] for t in tools] == ["local_read", "local_list"] and tools[0]["step"] == "allowlist"


async def test_a_write_asks_and_the_answers_are_honoured(w, client, fake_clara):
    w.ui.answers = ["allow_once", "refuse_once", "refuse_always"]
    calls = [("local_write", {"path": "one.txt", "content": "1"}), ("local_write", {"path": "two.txt", "content": "2"}),
             ("local_write", {"path": "three.txt", "content": "3"})]
    fake_clara.queue_turn(turn(), requests("t1", *calls), WaitResults(), done())
    await w.runner(client).run("write")
    posted = fake_clara.results["t1"]
    assert posted["call_0_0"].startswith("Created") and (w.work / "one.txt").read_text() == "1"
    assert posted["call_0_1"] == REFUSED_TEXT and not (w.work / "two.txt").exists()
    assert posted["call_0_2"] == REFUSED_TEXT and not (w.work / "three.txt").exists()
    assert len(w.ui.prompts) == 3 and w.ui.prompts[0].summary.startswith("Write")
    assert "New file, 1 lines" in w.ui.prompts[0].preview
    # "never" was kept: the same call is refused without asking
    fake_clara.queue_turn(turn("t2"), requests("t2", calls[2]), WaitResults(), done())
    await w.runner(client).run("again")
    assert fake_clara.results["t2"]["call_0_0"].startswith("Refused:") and len(w.ui.prompts) == 3


async def test_always_is_remembered_for_exactly_that_call(w, client, fake_clara):
    w.ui.answers = ["allow_always"]
    call = ("local_write", {"path": "x.txt", "content": "v1"})
    fake_clara.queue_turn(turn(), requests("t1", call), WaitResults(), done())
    await w.runner(client).run("1")
    assert any("Remembered" in n for n in w.ui.notes)
    fake_clara.queue_turn(turn("t2"), requests("t2", ("local_write", {"path": "x.txt", "content": "v2"})), WaitResults(), done())
    await w.runner(client).run("2")
    assert len(w.ui.prompts) == 1, "the second write to the same path should not ask"
    assert fake_clara.results["t2"]["call_0_0"].startswith("Wrote")  # a file this session created may be replaced
    assert (w.work / "x.txt").read_text() == "v2"


async def test_a_pattern_answer_is_kept_for_the_folder_and_nothing_asks_next_time(w, client, fake_clara):
    w.ui.answers = ["allow_pattern"]
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "ruff --version"})), WaitResults(), done())
    await w.runner(client).run("1")
    assert w.ui.prompts[0].proposal is not None and w.ui.prompts[0].proposal.patterns == (("ruff",),)
    assert any("Remembered in this folder: ruff:*" in n for n in w.ui.notes)
    fake_clara.queue_turn(turn("t2"), requests("t2", ("local_run", {"command": "ruff check . 2>&1 | tail -3"})),
                          WaitResults(), done())
    await w.runner(client).run("2")
    assert len(w.ui.prompts) == 1, "the second command is covered by the pattern"
    tools = [e for e in w.journal.read() if e["kind"] == "tool"]
    assert (tools[0]["answer"], tools[1]["step"]) == ("allow_pattern", "stored")


async def test_a_command_the_floor_or_a_tainted_turn_escalated_carries_no_proposal(w, client, fake_clara):
    w.ui.answers = ["allow_once"]
    fake_clara.queue_turn(turn(), {"type": "tool_start", "name": "web_fetch", "arguments": {}},
                          requests("t1", ("local_run", {"command": "ruff --version"})), WaitResults(), done())
    await w.runner(client).run("1")
    assert w.ui.prompts[0].tainted and w.ui.prompts[0].proposal is None


async def test_a_turn_that_read_outside_content_makes_everything_ask(w, client, fake_clara):
    w.ui.answers = ["allow_always", "allow_once"]
    call = ("local_write", {"path": "x.txt", "content": "v"})
    fake_clara.queue_turn(turn(), requests("t1", call), WaitResults(), done())
    await w.runner(client).run("1")
    assert len(w.ui.prompts) == 1
    for tool in ("web_fetch", "some_brand_new_tool"):  # a known outside reader, and one the console has never heard of
        w.ui.prompts.clear()
        w.ui.answers = ["allow_once"]
        fake_clara.queue_turn(turn("t2"), {"type": "tool_start", "name": tool, "arguments": {}}, requests("t2", ("local_write", {"path": "y.txt", "content": "v"})),
                              WaitResults(), done())
        await w.runner(client).run("2")
        assert len(w.ui.prompts) == 1 and w.ui.prompts[0].tainted, tool
        assert not w.ui.prompts[0].can_remember


async def test_a_safe_server_tool_does_not_taint(w, client, fake_clara):
    w.ui.answers = ["allow_always"]
    call = ("local_write", {"path": "x.txt", "content": "v"})
    fake_clara.queue_turn(turn(), requests("t1", call), WaitResults(), done())
    await w.runner(client).run("1")
    w.ui.prompts.clear()
    fake_clara.queue_turn(turn("t2"), {"type": "tool_start", "name": "remember", "arguments": {}},
                          requests("t2", call), WaitResults(), done())
    await w.runner(client).run("2")
    assert w.ui.prompts == []


async def test_an_attachment_counts_as_outside_content(w, client, fake_clara):
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "ls"})), WaitResults(), done())
    w.ui.answers = ["allow_once"]
    await w.runner(client).run("see the file", attachments=[Attachment("a.pdf", "QUJD")])
    assert len(w.ui.prompts) == 1 and w.ui.prompts[0].tainted  # even an allowlisted command asks
    assert fake_clara.last("/v1/chat/stream")["body"]["attachments"] == [{"name": "a.pdf", "data": "QUJD"}]


async def test_server_approvals_are_answered_while_the_stream_goes_on(w, client, fake_clara):
    fake_clara.add_approval(7)
    w.ui.answers = ["allow_once"]
    fake_clara.queue_turn(turn(), {"type": "approval", "approval": {"id": 7, "tool": "ws_delete", "summary": "Delete x", "level": "destructive",
                                                                  "reason": "asks"}},
                          WaitDecision(7), {"type": "token", "text": "gone"}, done())
    result = await w.runner(client).run("delete")
    assert result.outcome == "ok" and fake_clara.decisions == {7: "allow_once"}
    prompt = w.ui.prompts[0]
    assert (prompt.kind, prompt.tool, prompt.level, prompt.approval_id) == ("server", "ws_delete", "destructive", 7)
    assert any(e["kind"] == "approval" and e["answer"] == "allow_once" for e in w.journal.read())


async def test_an_approval_answered_on_another_client_closes_the_prompt(w, client, fake_clara):
    import asyncio
    w.ui.hold = asyncio.Event()  # the person never answers here
    fake_clara.add_approval(8)
    fake_clara.queue_turn(turn(), {"type": "approval", "approval": {"id": 8, "tool": "ws_delete", "summary": "Delete", "level": "destructive"}},
                          Sleep(0.3), {"type": "tool", "name": "ws_delete", "arguments": {}, "result": "deleted", "approval": 8},
                          done())
    result = await w.runner(client).run("delete")
    assert result.outcome == "ok" and w.ui.closed == [8] and fake_clara.decisions == {}


async def test_an_approval_already_answered_elsewhere_is_not_an_error(w, client, fake_clara):
    item = fake_clara.add_approval(9)
    item["status"] = "approved"  # the web site was faster
    w.ui.answers = ["allow_once"]
    fake_clara.queue_turn(turn(), {"type": "approval", "approval": {"id": 9, "tool": "ws_delete", "summary": "x", "level": "write"}},
                          Sleep(0.3), done())
    result = await w.runner(client).run("x")
    assert result.outcome == "ok" and any("already answered" in n for n in w.ui.notes)


async def test_cancelling_mid_turn_stops_the_connection_and_keeps_the_usage(w, client, fake_clara):
    fake_clara.queue_turn(turn(), {"type": "token", "text": "start"}, usage(5, 2), Hang())
    runner = w.runner(client)
    task = asyncio.create_task(runner.run("go"))
    for _ in range(100):
        if w.ui.events and len(w.ui.events) >= 3:
            break
        await asyncio.sleep(0.02)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if fake_clara.disconnects:
            break
        await asyncio.sleep(0.05)
    assert fake_clara.disconnects == 1
    totals = w.ledger.window("session", w.session.id)
    assert (totals.prompt, totals.completion, totals.cancelled_turns) == (5, 2, 1)  # BUG-11: a cancelled turn still counts


async def test_escape_stops_a_command_that_is_running_in_a_thread(w, client, fake_clara):
    w.ui.answers = ["allow_once"]
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "sleep 60"})), WaitResults(), done())
    runner = w.runner(client)
    task = asyncio.create_task(runner.run("run it"))
    for _ in range(100):
        if w.ctx.jobs is not None and w.ui.prompts:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)
    begun = time.monotonic()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - begun < 5


async def test_an_error_event_ends_the_turn_with_a_readable_error(w, client, fake_clara):
    fake_clara.queue_turn(turn(), usage(4, 1), {"type": "error", "status": 504, "code": "timeout", "message": "too slow"})
    result = await w.runner(client).run("x")
    assert result.outcome == "failed" and result.error is not None and result.error.code == "timeout"
    assert "background" in str(result.error)
    totals = w.ledger.window("all", "")
    assert (totals.prompt, totals.failed_turns) == (4, 1)
    assert any(e["kind"] == "error" for e in w.journal.read())


async def test_a_refused_request_is_a_failed_turn_and_the_notes_survive(w, client, fake_clara):  # BUG-7
    w.session.pending_notes.append("$ ls\nfile.txt")
    fake_clara.queue_turn(HttpError(503, "unavailable", "Clara is stopping."))
    result = await w.runner(client).run("hello")
    assert result.outcome == "failed" and result.error.code == "unavailable"
    assert w.session.pending_notes == ["$ ls\nfile.txt"], "the notes must stay queued when the request failed"
    fake_clara.queue_turn(turn(), done())
    await w.runner(client).run("hello again")
    assert w.session.pending_notes == []
    assert fake_clara.last("/v1/chat/stream")["body"]["message"] == "$ ls\nfile.txt\n\nhello again"


async def test_a_wrong_token_is_a_failed_turn_with_one_line(w, fake_clara):
    async with ClaraClient(fake_clara.url, "clara_x_wrong") as bad:
        result = await w.runner(bad).run("hi")
    assert result.outcome == "failed" and "--save-token" in str(result.error)


async def test_a_retry_as_a_new_version_carries_no_tools(w, client, fake_clara):
    w.session.conversation = CID
    fake_clara.queue_turn(turn(), done())
    await w.runner(client).run("the same question", fork_of=7)  # the API refuses an empty message even with fork_of
    body = fake_clara.last("/v1/chat/stream")["body"]
    assert body["fork_of"] == 7 and "tools" not in body and body["conversation"] == CID and body["message"] == "the same question"


async def test_the_round_budget_refuses_what_is_left_when_time_runs_out(w, client, fake_clara, monkeypatch):
    monkeypatch.setattr(turn_module, "ROUND_BUDGET", 6.0)
    w.ui.hold = asyncio.Event()
    calls = [("local_write", {"path": "a.txt", "content": "1"}), ("local_write", {"path": "b.txt", "content": "2"})]
    fake_clara.queue_turn(turn(), requests("t1", *calls), WaitResults(), done())
    begun = time.monotonic()
    await w.runner(client).run("x")
    assert time.monotonic() - begun < 8
    assert set(fake_clara.results["t1"].values()) == {NO_ANSWER_TEXT}
    assert not (w.work / "a.txt").exists() and not (w.work / "b.txt").exists()
    assert w.ui.prompts[0].seconds_left is not None and w.ui.prompts[0].seconds_left <= 6.0


async def test_tools_are_shaped_before_they_go_to_clara(w, client, fake_clara):
    w.ctx.result_limit = 1500
    (w.work / "wide.txt").write_text("x" * 100 + "\n")
    leaked = "clara_ab12cd_0123456789abcdefghijklmnopqrstuv"
    (w.work / "note.txt").write_text(f"my token is {leaked}\nPASSWORD=keepme\n" + "pad\n" * 1000)
    fake_clara.queue_turn(turn(), requests("t1", ("local_read", {"path": "note.txt"}), ("local_nothing", {}),
                                           ("local_read", {"path": 5}), ("local_read", {"path": "missing"})),
                          WaitResults(), done())
    await w.runner(client).run("x")
    posted = fake_clara.results["t1"]
    assert leaked not in posted["call_0_0"] and "[redacted]" in posted["call_0_0"]
    assert "PASSWORD=keepme" in posted["call_0_0"], "file content must reach the model unchanged, only the token is hidden"
    assert len(posted["call_0_0"]) < 1800 and "characters left out" in posted["call_0_0"]
    assert posted["call_0_1"] == "local_nothing is not available in this session."
    assert posted["call_0_2"] == "Invalid arguments: path must be string."
    assert "No such file" in posted["call_0_3"]


async def test_the_journal_redacts_what_it_records(w, client, fake_clara):
    w.ui.answers = ["allow_once"]
    fake_clara.queue_turn(turn(), requests("t1", ("local_run", {"command": "echo hi # API_KEY=sk-abcdefghijklmnopqrstuvwxyz"})),
                          WaitResults(), done())
    await w.runner(client).run("x")
    text = w.journal.path.read_text()
    assert "sk-abcdefghijklmnopqrst" not in text and "[redacted]" in text


async def test_a_sub_agent_runner_is_offered_only_the_read_tools(w, client, fake_clara):
    fake_clara.queue_turn(turn(), done())
    await w.runner(client, subagent=True, agent="sub:1").run("research")
    names = {t["function"]["name"] for t in fake_clara.last("/v1/chat/stream")["body"]["tools"]}
    assert names == {"local_list", "local_read", "local_search", "local_git", "local_pdf_to_markdown"} - ({"local_git"} if not w.ctx.git else set())
    assert w.ledger.window("all", "").by_agent.get("sub:1", 0) == 0 and w.store.list() == []  # not saved


async def test_a_tool_that_crashes_does_not_crash_the_turn(w, client, fake_clara):
    import dataclasses

    from custom_console.core.tools.registry import ToolRegistry

    def boom(ctx, args):
        raise RuntimeError("kaput " + "x" * 500)

    w.registry = ToolRegistry([dataclasses.replace(w.registry.get("local_list"), run=boom)])
    fake_clara.queue_turn(turn(), requests("t1", ("local_list", {})), WaitResults(), done())
    result = await w.runner(client).run("x")
    assert result.outcome == "ok" and fake_clara.results["t1"]["call_0_0"].startswith("The tool failed (RuntimeError: kaput")
    assert len(fake_clara.results["t1"]["call_0_0"]) < 300


async def test_direct_call_path_for_the_prompt_content(w):
    answers = []
    w.ui.answers = lambda p: (answers.append(p), "allow_once")[1]
    (w.work / "f.txt").write_text("old\n")
    await w.call("local_read", path="f.txt")
    out = await w.call("local_edit", path="f.txt", old_text="old", new_text="new")
    assert out.startswith("Edited"), out
    prompt = answers[0]
    assert prompt.undoable and "-old" in prompt.preview and "+new" in prompt.preview
    assert prompt.can_remember and prompt.paths[0].virtual.endswith("f.txt")
    run_out = await w.call("local_run", command="python -c 1")
    assert not answers[1].can_remember and "cannot be undone" in answers[1].preview and run_out.startswith("exit code")


async def test_the_details_of_a_prompt_are_built_only_when_a_prompt_is_shown(w, client, fake_clara, monkeypatch):
    """PERF-3: descriptions were built even when no question was asked."""
    from custom_console.core.turn import TurnRunner

    built: list[dict] = []
    original = TurnRunner._preview

    def counting(self, previewer, args):
        built.append(args)
        return original(self, previewer, args)

    monkeypatch.setattr(TurnRunner, "_preview", counting)
    (w.work / "a.txt").write_text("x\n")
    w.ui.answers = ["allow_always"]
    fake_clara.queue_turn(turn(), requests("t1", ("local_read", {"path": "a.txt"}),
                                           ("local_write", {"path": "b.txt", "content": "1"})), WaitResults(), done())
    await w.runner(client).run("1")
    assert len(w.ui.prompts) == 1 and len(built) == 1  # the read never asked; the write asked once
    built.clear()
    fake_clara.queue_turn(turn("t2"), requests("t2", ("local_write", {"path": "b.txt", "content": "2"})), WaitResults(), done())
    await w.runner(client).run("2")
    assert len(w.ui.prompts) == 1 and built == []  # remembered: no question, so no details either
