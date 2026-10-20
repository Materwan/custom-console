"""Live smoke tests: a real Clara and a real token (~/.config/clara/token). Run by hand, never in CI:

    uv run pytest -m live tests/live -v

They make short real model calls. Conversations they create are deleted at the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from custom_console.clara import DEFAULT_URL, ClaraClient, ClaraError, read_token
from custom_console.clara.events import Token, ToolRequests, TurnStarted
from custom_console.clara.types import ChatBody
from tests.conftest import REAL_HOME

pytestmark = pytest.mark.live

ECHO = {"type": "function", "function": {
    "name": "local_echo", "description": "Return the text it is given, in capitals.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}


@pytest.fixture
async def live():
    path = Path(os.environ.get("CLARA_TOKEN_FILE") or REAL_HOME / ".config" / "clara" / "token")
    async with ClaraClient(os.environ.get("CLARA_URL", DEFAULT_URL), read_token(path), read_timeout=120) as client:
        await client.me()
        yield client


async def test_who_am_i_and_the_pickers(live):
    me = await live.me()
    assert me.client == "console"
    models = await live.models()
    assert models.models and models.default
    assert {p.id for p in await live.packs()} >= {"web"}
    await live.projects()
    rules = await live.rules()
    assert rules.defaults and rules.tainted_rule is not None
    assert await live.approvals() is not None
    tested = await live.test_rule("ws_write", {"path": "x.txt", "content": "x"})
    assert tested["effect"] in ("allow", "ask", "refuse")


async def test_a_local_tool_round_trip(live):
    body = ChatBody(message="Call the tool local_echo with the text 'ping', then say what it returned.",
                    tools=(ECHO,), incognito=True)
    rounds, answer = 0, ""
    async with contextlib.aclosing(live.chat_stream(body)) as stream:
        async for event in stream:
            if isinstance(event, ToolRequests):
                rounds += 1
                await live.send_tool_results(event.turn, {c.id: str(c.arguments.get("text", "")).upper() for c in event.calls})
            elif isinstance(event, Token):
                answer += event.text
    assert rounds >= 1 and "PING" in answer.upper()


async def test_cancelling_a_stored_turn_stops_it_on_the_server(live):
    """The open point of the plan: does Clara stop a turn when the client disconnects?"""
    conversation = await live.create_conversation(title="console live test (deletable)")
    try:
        got_text = asyncio.Event()

        async def run() -> None:
            body = ChatBody(message="Count from 1 to 400, one number per line, nothing else.", conversation=conversation.id)
            async with contextlib.aclosing(live.chat_stream(body)) as stream:
                async for event in stream:
                    if isinstance(event, Token):
                        got_text.set()

        task = asyncio.create_task(run())
        await asyncio.wait_for(got_text.wait(), 90)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(4)
        first = await live.messages(conversation.id)
        await asyncio.sleep(6)
        second = await live.messages(conversation.id)
        answers = [m for m in second if m.role == "assistant"]
        assert answers, "Clara stored nothing of the interrupted turn"
        assert "interrupted" in answers[-1].content.lower(), answers[-1].content[-200:]
        assert "400" not in answers[-1].content.split("[This answer")[0], "the turn ran to its end after the disconnect"
        assert [m.content for m in first] == [m.content for m in second], "the turn kept running after the disconnect"
    finally:
        with contextlib.suppress(ClaraError):
            await live.delete_conversation(conversation.id)


# There is deliberately no live test of an approval round: the server tools that ask by default change the
# workspace, and `ws_write` (allowed without asking) creates files. The approval answer path is covered by the contract
# tests against the fake server; here only the read-only approval and rule routes are used (see the first test).


async def test_a_turn_event_names_the_conversation_and_the_model(live):
    async with contextlib.aclosing(live.chat_stream(ChatBody(message="Say OK.", incognito=True))) as stream:
        first = await anext(stream)
    assert isinstance(first, TurnStarted) and first.model and first.incognito
