"""Contract tests: the Clara client against a fake Clara over real HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from custom_console.clara import ClaraClient, ClaraError, ConnectionFailed, StreamSilent, describe_error
from custom_console.clara.events import (
    ApprovalRequested,
    Compacted,
    Done,
    ErrorEvent,
    Retrying,
    Thinking,
    Token,
    ToolDone,
    ToolRequests,
    ToolStart,
    TurnStarted,
    UnknownEvent,
    Usage,
    Warning,
)
from custom_console.clara.types import ChatBody
from tests.fake_clara.server import (
    BeforeHeaders,
    Drop,
    Hang,
    HttpError,
    Keepalive,
    Raw,
    Sleep,
    WaitDecision,
    WaitResults,
)

TURN = {"type": "turn", "id": "t1", "conversation": "c0123456789abcdef", "model": "fake:small", "incognito": False,
        "local_only": False}
DONE = {"type": "done", "reply": "ok", "conversation": "c0123456789abcdef", "tools": [], "message_id": 5,
        "versions": [5], "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        "context": {"tokens": 12, "window": 1000, "percent": 1.2}, "model": "fake:small", "tainted": False}


async def collect(client: ClaraClient, body: ChatBody | None = None) -> list:
    events = []
    async with contextlib.aclosing(client.chat_stream(body or ChatBody(message="hi"))) as stream:
        async for event in stream:
            events.append(event)
    return events


async def test_every_request_carries_the_token_and_the_client_label(client, fake_clara):
    await client.me()
    await client.models()
    for request in fake_clara.requests:
        assert request["headers"]["authorization"] == f"Bearer {fake_clara.token}"
        assert request["headers"]["x-clara-client"] == "console"


async def test_a_wrong_token_is_one_clear_line(fake_clara):
    async with ClaraClient(fake_clara.url, "clara_x_wrong") as bad:
        with pytest.raises(ClaraError) as caught:
            await bad.me()
    assert caught.value.code == "unauthorized" and caught.value.status == 401
    assert "--save-token" in str(caught.value)


@pytest.mark.parametrize(("status", "code", "expected"), [
    (413, "too_large", "/compact"),
    (422, "unprocessable", "The message is empty."),
    (409, "conflict", "Nothing to compact."),
    (429, "rate_limited", "retry"),
    (502, "model_failed", "/model"),
    (503, "unavailable", "stopping"),
    (504, "timeout", "background"),
])
async def test_each_error_code_has_a_line_and_a_next_step(client, fake_clara, status, code, expected):
    fake_clara.queue_turn(HttpError(status, code, {"unavailable": "Clara is stopping.", "conflict": "Nothing to compact.",
                                                   "unprocessable": "The message is empty."}.get(code, ""),
                                    {"Retry-After": "7"} if status == 429 else {}))
    with pytest.raises(ClaraError) as caught:
        await collect(client)
    assert caught.value.code == code and caught.value.status == status
    assert expected.lower() in str(caught.value).lower()
    if status == 429:
        assert caught.value.retry_after == 7.0


async def test_events_come_typed_in_order(client, fake_clara):
    fake_clara.queue_turn(
        TURN, {"type": "thinking", "text": "hmm"}, {"type": "token", "text": "He"}, {"type": "token", "text": "llo"},
        {"type": "retrying", "message": "slow", "model": "fake:big"},
        {"type": "tool_start", "name": "web_search", "arguments": {"q": "x"}},
        {"type": "tool", "name": "web_search", "arguments": {"q": "x"}, "result": "r", "truncated": False},
        {"type": "usage", "prompt_tokens": 3, "completion_tokens": 4, "cached_tokens": 1},
        {"type": "compacted", "before": 80.5, "after": 20.0}, {"type": "warning", "message": "careful"}, DONE)
    events = await collect(client)
    assert [type(e) for e in events] == [TurnStarted, Thinking, Token, Token, Retrying, ToolStart, ToolDone, Usage,
                                         Compacted, Warning, Done]
    assert events[0].conversation == "c0123456789abcdef"
    assert events[7] == Usage(3, 4, 1)
    done = events[-1]
    assert done.message_id == 5 and done.context_percent == 1.2 and done.prompt_tokens == 10


async def test_unknown_events_unknown_fields_and_other_lines_are_ignored(client, fake_clara):
    fake_clara.queue_turn(
        TURN, Keepalive(), Raw("id: 3\n"), Raw(": a comment\n\n"), Raw("data: not json\n\n"), Raw("data: [1, 2]\n\n"),
        {"type": "from_the_future", "x": 1}, {"type": "token", "text": "a", "extra": {"deep": True}}, DONE)
    events = await collect(client)
    assert [type(e) for e in events] == [TurnStarted, UnknownEvent, Token, Done]
    assert events[1].type == "from_the_future"


async def test_a_malformed_known_event_becomes_unknown_not_a_crash(client, fake_clara):
    fake_clara.queue_turn(TURN, {"type": "tool_requests", "turn": "t1", "calls": [{"name": "local_read"}]}, DONE)
    events = await collect(client)
    assert isinstance(events[1], UnknownEvent)


async def test_an_error_event_is_delivered_as_an_event(client, fake_clara):
    fake_clara.queue_turn(TURN, {"type": "error", "status": 504, "code": "timeout", "message": "too slow"})
    events = await collect(client)
    assert events[-1] == ErrorEvent(504, "timeout", "too slow")
    assert events[-1].as_error().code == "timeout"


async def test_silence_longer_than_the_read_timeout_is_a_dead_connection(fake_clara):
    fake_clara.queue_turn(TURN, Hang())
    async with ClaraClient(fake_clara.url, fake_clara.token, read_timeout=0.4) as short:
        with pytest.raises(StreamSilent):
            await collect(short)


async def test_keepalives_keep_a_slow_turn_alive(fake_clara):
    fake_clara.queue_turn(TURN, Sleep(0.25), Keepalive(), Sleep(0.25), Keepalive(), Sleep(0.25), DONE)
    async with ClaraClient(fake_clara.url, fake_clara.token, read_timeout=0.4) as short:
        events = await collect(short)
    assert isinstance(events[-1], Done)


async def test_the_connection_dropping_is_reported(client, fake_clara):
    fake_clara.queue_turn(TURN, Drop())
    with pytest.raises(ConnectionFailed):
        await collect(client)


async def test_nobody_listening_is_reported():
    async with ClaraClient("http://127.0.0.1:1", "clara_a_b") as nowhere:
        with pytest.raises(ConnectionFailed) as caught:
            await nowhere.me()
    assert "CLARA_URL" in str(caught.value)


async def test_tool_requests_round_trip_on_the_same_stream(client, fake_clara):
    fake_clara.queue_turn(
        TURN, {"type": "token", "text": "Let me look. "},
        {"type": "tool_requests", "turn": "t1", "calls": [{"id": "call_0_0", "name": "local_read", "arguments": {"path": "a"}},
                                                          {"id": "call_0_1", "name": "local_list", "arguments": {}}]},
        WaitResults(), {"type": "token", "text": "Done."}, DONE)
    seen = []
    async with contextlib.aclosing(client.chat_stream(ChatBody(message="go"))) as stream:
        async for event in stream:
            seen.append(event)
            if isinstance(event, ToolRequests):
                assert [c.id for c in event.calls] == ["call_0_0", "call_0_1"]
                await client.send_tool_results(event.turn, {"call_0_0": "contents", "call_0_1": "listing"})
    assert isinstance(seen[-1], Done)
    assert fake_clara.results["t1"] == {"call_0_0": "contents", "call_0_1": "listing"}
    assert len(fake_clara.requests_to("/v1/turns/t1/tool-results")) == 1  # one post for the whole round


async def test_results_for_an_unknown_turn_are_refused(client):
    with pytest.raises(ClaraError) as caught:
        await client.send_tool_results("nope", {"a": "b"})
    assert caught.value.status == 404


async def test_an_approval_is_answered_while_the_stream_waits(client, fake_clara):
    fake_clara.add_approval(7)
    fake_clara.queue_turn(
        TURN, {"type": "approval", "approval": {"id": 7, "tool": "write_file", "summary": "Write x", "level": "write"}},
        WaitDecision(7), {"type": "token", "text": "written"}, DONE)
    async with contextlib.aclosing(client.chat_stream(ChatBody(message="go"))) as stream:
        async for event in stream:
            if isinstance(event, ApprovalRequested):
                assert event.approval.id == 7 and event.approval.level == "write"
                await client.decide(7, "allow_once")
    assert fake_clara.decisions == {7: "allow_once"}
    with pytest.raises(ClaraError) as caught:
        await client.decide(7, "allow_once")
    assert caught.value.code == "conflict"
    with pytest.raises(ValueError):
        await client.decide(7, "maybe")


async def test_cancelling_mid_stream_closes_the_connection(client, fake_clara):
    fake_clara.queue_turn(TURN, {"type": "token", "text": "start"}, Hang())
    got_first = asyncio.Event()

    async def run() -> None:
        async with contextlib.aclosing(client.chat_stream(ChatBody(message="go"))) as stream:
            async for event in stream:
                if isinstance(event, Token):
                    got_first.set()

    task = asyncio.create_task(run())
    await asyncio.wait_for(got_first.wait(), 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    for _ in range(100):
        if fake_clara.disconnects:
            break
        await asyncio.sleep(0.05)
    assert fake_clara.disconnects == 1


async def test_cancelling_before_the_headers_arrive_closes_the_connection(client, fake_clara):
    """BUG-8: Esc before the first byte must still stop the turn on the server."""
    fake_clara.queue_turn(BeforeHeaders(10))
    task = asyncio.create_task(collect(client))
    for _ in range(100):  # the request has reached the server
        if fake_clara.requests_to("/v1/chat/stream"):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    for _ in range(100):
        if fake_clara.cancelled_before_headers:
            break
        await asyncio.sleep(0.05)
    assert fake_clara.cancelled_before_headers == 1


async def test_the_body_is_built_from_typed_fields_and_keeps_zero_and_false(client, fake_clara):
    body = ChatBody(message="hi", fork_of=0, incognito=False, project=0, timezone="Europe/Paris")
    assert body.to_json() == {"message": "hi", "fork_of": 0, "incognito": False, "project": 0, "timezone": "Europe/Paris"}
    await collect(client, ChatBody(message="x", incognito=True, local_only=False))
    sent = fake_clara.last("/v1/chat/stream")["body"]
    assert sent == {"message": "x", "incognito": True, "local_only": False}


@pytest.mark.parametrize("body", [
    ChatBody(message="  "), ChatBody(message="x", conversation="not-an-id"),
    ChatBody(message="x", tools=tuple({"function": {"name": f"t{i}"}} for i in range(65))),
    ChatBody(message="x", instructions="y" * 20_001), ChatBody(message="x" * 2_000_001),
])
async def test_a_body_clara_would_refuse_is_refused_before_sending(client, fake_clara, body):
    with pytest.raises(ClaraError) as caught:
        await collect(client, body)
    assert caught.value.code == "unprocessable"
    assert not fake_clara.requests_to("/v1/chat/stream")


async def test_an_empty_text_with_an_attachment_is_fine(client, fake_clara):
    from custom_console.clara.types import Attachment
    await collect(client, ChatBody(message="", attachments=(Attachment("a.pdf", "QUJD"),)))
    assert fake_clara.last("/v1/chat/stream")["body"]["attachments"] == [{"name": "a.pdf", "data": "QUJD"}]


async def test_the_token_is_in_no_log_and_no_repr(client, fake_clara, caplog):
    caplog.set_level(logging.DEBUG)
    await client.me()
    await collect(client)
    assert fake_clara.token not in repr(client)
    assert fake_clara.token not in caplog.text


async def test_rest_routes(client, fake_clara):
    me = await client.me()
    assert me.client == "console" and me.approvals == "all"
    models = await client.models()
    assert [m.ref for m in models.models] == ["fake:small", "fake:big"] and models.default == "fake:small"
    assert models.models[1].vision is True
    assert [p.id for p in await client.packs()] == ["web", "second_brain"]
    assert (await client.projects())[0].name == "Demo"
    conversation = await client.create_conversation(title="T", incognito=True, packs=["web"])
    assert conversation.incognito and conversation.id.startswith("c")
    assert [c.id for c in await client.conversations()] == [conversation.id]
    assert (await client.change_conversation(conversation.id, title="New")).title == "New"
    assert await client.messages(conversation.id) == ()
    assert await client.compact(conversation.id) == (61.5, 12.0)
    assert await client.title(conversation.id) == "A title"
    await client.switch_version(conversation.id, 3)
    await client.delete_conversation(conversation.id)
    with pytest.raises(ClaraError):
        await client.conversation(conversation.id)
    assert (await client.rules()).tainted_rule is True
    assert (await client.test_rule("write_file", {"path": "x"}))["effect"] == "ask"
    assert await client.approvals() == ()


def test_describe_error_covers_every_documented_code():
    for code in ("unauthorized", "too_large", "unprocessable", "conflict", "rate_limited", "model_failed", "unavailable",
                 "timeout"):
        assert str(describe_error(code, "server words")).strip()
    assert str(describe_error("brand_new", "", 418)) == "Clara answered with an error (418)."
