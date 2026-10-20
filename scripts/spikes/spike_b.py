"""Spike B / Gate 0: one turn with one local tool, streamed with httpx, and a cancel in the middle.

    python scripts/spikes/spike_b.py --fake                  # against the fake Clara (what CI can run)
    python scripts/spikes/spike_b.py [--url URL] [--token-file PATH]     # against a real Clara
    python scripts/spikes/spike_b.py --cancel                # also: stop a second turn after its first token
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from custom_console.clara import DEFAULT_URL, ClaraClient, ClaraError, read_token  # noqa: E402
from custom_console.clara.events import Done, Token, ToolRequests  # noqa: E402
from custom_console.clara.types import ChatBody  # noqa: E402

ECHO = {"type": "function", "function": {
    "name": "local_echo", "description": "Return the text it is given, in capitals.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}


async def one_turn(client: ClaraClient) -> bool:
    body = ChatBody(message="Call the tool local_echo with the text 'ping', then tell me what it returned.",
                    tools=(ECHO,), incognito=True, instructions="You are testing a client. Use the tool as asked.")
    rounds = 0
    answer = ""
    begun = time.monotonic()
    async with contextlib.aclosing(client.chat_stream(body)) as stream:
        async for event in stream:
            if isinstance(event, ToolRequests):
                rounds += 1
                results = {c.id: str(c.arguments.get("text", "")).upper() for c in event.calls}
                print(f"  tool_requests: {[c.name for c in event.calls]} -> {results}")
                await client.send_tool_results(event.turn, results)
            elif isinstance(event, Token):
                answer += event.text
            elif isinstance(event, Done):
                print(f"  done after {time.monotonic() - begun:.1f}s, model {event.model}, tools {list(event.tools)}")
            elif hasattr(event, "code"):
                print(f"  error event: {event}")
    print(f"  answer: {answer.strip()[:120]!r}")
    return rounds >= 1 and "PING" in answer.upper()


async def cancelled_turn(client: ClaraClient) -> bool:
    body = ChatBody(message="Count slowly from 1 to 300, one number per line.", incognito=True)
    got = asyncio.Event()

    async def run() -> None:
        async with contextlib.aclosing(client.chat_stream(body)) as stream:
            async for event in stream:
                if isinstance(event, Token):
                    got.set()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(got.wait(), 60)
    except TimeoutError:
        task.cancel()
        return False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    print("  cancelled after the first token; check the server log that the turn stopped")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token-file")
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    fake = None
    if args.fake:
        from tests.fake_clara.server import FakeClara, WaitResults
        fake = FakeClara()
        fake.start()
        fake.queue_turn(
            {"type": "turn", "id": "t1", "conversation": None, "model": "fake:small"},
            {"type": "tool_requests", "turn": "t1", "calls": [{"id": "c1", "name": "local_echo", "arguments": {"text": "ping"}}]},
            WaitResults(), {"type": "token", "text": "It returned PING."},
            {"type": "done", "reply": "It returned PING.", "tools": ["local_echo"], "model": "fake:small"})
        url, token = fake.url, fake.token
    else:
        url = args.url
        path = Path(args.token_file).expanduser() if args.token_file else Path.home() / ".config" / "clara" / "token"
        token = read_token(path)
    ok = True
    try:
        async with ClaraClient(url, token) as client:
            me = await client.me()
            print(f"connected to {url} as token {me.token!r} (client {me.client}, approvals {me.approvals})")
            print("turn with a local tool:")
            ok &= await one_turn(client)
            if args.cancel and not args.fake:
                print("cancel in the middle:")
                ok &= await cancelled_turn(client)
    except ClaraError as error:
        print(f"FAILED: {error}")
        ok = False
    finally:
        if fake:
            fake.stop()
    print("SPIKE B", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
