"""The usage ledger: token counts from Clara's `usage` events, one JSON line each.

A `usage` event is written the moment it arrives, labelled with the agent that caused it (the main one or a
sub-agent), so a turn that later fails or is cancelled still counts. `end_turn` adds a line saying how a turn ended.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Totals:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    calls: int = 0  # model rounds (usage events)
    turns: int = 0
    failed_turns: int = 0
    cancelled_turns: int = 0
    by_agent: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # agent -> prompt + completion
    by_model: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def total(self) -> int:
        return self.prompt + self.completion


class Ledger:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock

    def _append(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def record(self, session: str, *, prompt: int, completion: int, cached: int = 0, model: str = "",
               agent: str = "main", conversation: str | None = None) -> None:
        self._append({"t": round(self._clock(), 3), "kind": "usage", "session": session, "conversation": conversation,
                      "model": model, "agent": agent, "prompt": prompt, "completion": completion, "cached": cached})

    def end_turn(self, session: str, outcome: str, *, agent: str = "main") -> None:
        """`outcome` is `ok`, `failed` or `cancelled`."""
        self._append({"t": round(self._clock(), 3), "kind": "turn", "session": session, "agent": agent, "outcome": outcome})

    def totals(self, *, since: float | None = None, session: str | None = None) -> Totals:
        totals = Totals()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return totals
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a half-written last line
            if not isinstance(item, dict) or (since is not None and float(item.get("t", 0)) < since):
                continue
            if session is not None and item.get("session") != session:
                continue
            if item.get("kind") == "usage":
                used = int(item.get("prompt", 0)) + int(item.get("completion", 0))
                totals.prompt += int(item.get("prompt", 0))
                totals.completion += int(item.get("completion", 0))
                totals.cached += int(item.get("cached", 0))
                totals.calls += 1
                totals.by_agent[str(item.get("agent", "main"))] += used
                totals.by_model[str(item.get("model", ""))] += used
            elif item.get("kind") == "turn":
                totals.turns += 1
                totals.failed_turns += item.get("outcome") == "failed"
                totals.cancelled_turns += item.get("outcome") == "cancelled"
        return totals

    def window(self, name: str, session: str) -> Totals:
        """`session`, `24h`, `7d` or `all`."""
        now = self._clock()
        if name == "session":
            return self.totals(session=session)
        if name in ("24h", "day"):
            return self.totals(since=now - 86_400)
        if name in ("7d", "week"):
            return self.totals(since=now - 7 * 86_400)
        if name == "all":
            return self.totals()
        raise ValueError("The window is session, 24h, 7d or all.")
