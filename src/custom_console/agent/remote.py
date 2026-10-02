"""Running a turn on the Clara server while the tools run here.

The server streams the model's answer; when the model wants a tool it sends a `tool_requests`
event, the console runs the tools on this computer (asking the user first when it must) and posts
the results, and the same stream goes on with the next model round.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .clara import ClaraClient, ClaraError
from .schema import tool_schema

Tool = Callable[..., Any]
Execute = Callable[[str, Dict[str, Any]], str]  # (tool name, arguments) -> what the model is told


@dataclass
class RemoteAgent:
    """What the console lends to the server for a turn: its tools and its instructions."""

    client: ClaraClient
    tools: Callable[[], List[Tool]]  # the tools that are on
    instructions: Callable[[], str]  # the agent's system prompt

    def schemas(self, tools: Optional[List[Tool]] = None) -> List[Dict[str, Any]]:
        return [tool_schema(tool) for tool in (self.tools() if tools is None else tools)]


def run_remote_turn(
    client: ClaraClient,
    body: Dict[str, Any],
    execute: Execute,
    *,
    on_text: Optional[Callable[[str], None]] = None,
    on_usage: Optional[Callable[[int, int], None]] = None,
    on_note: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> Optional[Dict[str, Any]]:
    """Run one turn and return its final `done` event (None if `cancel` was set meanwhile).

    `on_usage(prompt_tokens, completion_tokens)` is told after each model round, `on_note(text)`
    of what the server did on its own (compaction...). Raises :class:`ClaraError`.
    """
    stream = client.stream_turn(body)
    try:
        for event in stream:
            if cancel is not None and cancel.is_set():
                return None
            kind = event.get("type")
            if kind == "token":
                if on_text:
                    on_text(event["text"])
            elif kind == "usage":
                if on_usage:
                    on_usage(event.get("prompt_tokens", 0), event.get("completion_tokens", 0))
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
                return event
        if cancel is not None and cancel.is_set():
            return None
        raise ClaraError("The Clara server closed the stream before the answer was complete.")
    finally:
        stream.close()
