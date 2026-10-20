"""Server-sent events: the lines of a response become typed events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .events import Event, parse_event


def data_of(line: str) -> dict[str, Any] | None:
    """The JSON object of a `data:` line; None for anything else (comments such as `: keepalive`, `event:` and `id:`
    lines, blank lines, and data that is not a JSON object)."""
    if not line.startswith("data:"):
        return None
    text = line[5:]
    if text.startswith(" "):
        text = text[1:]
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def read_events(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    async for line in lines:
        data = data_of(line)
        if data is not None:
            yield parse_event(data)
