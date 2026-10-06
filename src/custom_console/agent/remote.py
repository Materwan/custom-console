"""Running a turn on the Clara server while the tools run here.

The server streams the model's answer; when the model wants a tool it sends a `tool_requests`
event, the console runs the tools on this computer (asking the user first when it must) and posts
the results, and the same stream goes on with the next model round.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from .clara import ClaraClient, ClaraError
from .schema import tool_schema

Tool = Callable[..., Any]
Execute = Callable[[str, Dict[str, Any]], str]  # (tool name, arguments) -> what the model is told
CANCEL_POLL = 0.1  # seconds between two looks at the cancel flag while the stream is silent


@dataclass
class RemoteAgent:
    """What the console lends to the server for a turn: its tools and its instructions."""

    client: ClaraClient
    tools: Callable[[], List[Tool]]  # the tools that are on
    instructions: Callable[[], str]  # the agent's system prompt

    def schemas(self, tools: Optional[List[Tool]] = None) -> List[Dict[str, Any]]:
        return [tool_schema(tool) for tool in (self.tools() if tools is None else tools)]


_END = object()


def _pump(stream: Iterator[Dict[str, Any]], events: "queue.Queue[Any]", cancel: Optional[threading.Event]) -> None:
    """Read the stream into `events` as ``(item, late)`` pairs: an event, then `_END` (or the exception
    that stopped it); `late` when the stop was asked before it arrived (it is then not shown)."""

    def late() -> bool:
        return cancel is not None and cancel.is_set()

    try:
        for event in stream:
            events.put((event, late()))
        events.put((_END, late()))
    except BaseException as error:  # handed to the turn, which raises it
        events.put((error, late()))


def run_remote_turn(
    client: ClaraClient,
    body: Dict[str, Any],
    execute: Execute,
    *,
    on_text: Optional[Callable[[str], None]] = None,
    on_usage: Optional[Callable[[int, int], None]] = None,
    on_note: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
    on_server_tool: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> Optional[Dict[str, Any]]:
    """Run one turn and return its final `done` event (None if `cancel` was set meanwhile).

    `on_usage(prompt_tokens, completion_tokens)` is told after each model round, `on_note(text)`
    of what the server did on its own (compaction...), `on_thinking(text)` of the model's reasoning
    and `on_server_tool(name, arguments, result)` of a tool the server ran itself (remember,
    web_search...). Raises :class:`ClaraError`.

    The stream is read by a thread of its own, so that setting `cancel` ends the turn at once even
    while the server is silent (a read blocked on a socket cannot be woken on Windows): the
    connection is then shut down, which makes the server give the turn up.
    """
    stream = client.stream_turn(body)
    events: "queue.Queue[Any]" = queue.Queue()
    threading.Thread(target=_pump, args=(stream, events, cancel), name="turn-stream", daemon=True).start()
    finished = False
    try:
        while True:
            try:
                event, late = events.get(timeout=CANCEL_POLL)
            except queue.Empty:
                if cancel is not None and cancel.is_set():
                    return None
                continue
            if late:  # what came once the user had stopped the turn
                return None
            if event is _END:
                finished = True
                raise ClaraError("The Clara server closed the stream before the answer was complete.")
            if isinstance(event, BaseException):
                finished = True
                raise event
            kind = event.get("type")
            if kind == "token":
                if on_text:
                    on_text(event["text"])
            elif kind == "thinking":
                if on_thinking:
                    on_thinking(str(event.get("text", "")))
            elif kind == "usage":
                if on_usage:
                    on_usage(event.get("prompt_tokens", 0), event.get("completion_tokens", 0))
            elif kind == "tool":
                if on_server_tool:
                    on_server_tool(str(event.get("name", "?")), event.get("arguments") or {}, str(event.get("result", "")))
            elif kind in ("tool_requests", "done") and cancel is not None and cancel.is_set():
                return None  # what arrived before the stop is shown, but a stopped turn acts no more
            elif kind == "tool_requests":
                results = [
                    {"id": call["id"], "content": execute(call["name"], call.get("arguments") or {})}
                    for call in event["calls"]
                ]
                if cancel is not None and cancel.is_set():
                    return None
                client.send_results(event["turn"], results)
            elif kind == "compacted":
                if on_note:
                    on_note(f"Conversation compacted ({event['before']:.0f}% → {event['after']:.0f}% of the context).")
            elif kind == "warning":
                if on_note:
                    on_note(str(event.get("message", "")))
            elif kind == "error":
                raise ClaraError(str(event.get("message", "the server reported an error")))
            elif kind == "done":
                finished = True  # the server ends the stream itself
                return event
    except Exception:
        if cancel is not None and cancel.is_set():
            return None  # whatever broke, the turn was being stopped
        raise
    finally:
        if not finished:
            client.abort_stream()  # stopped, or left early: the server gives the turn up
