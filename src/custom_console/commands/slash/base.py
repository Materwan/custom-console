"""What every slash command shares.

A slash command is one `async def (ctx, args)`: it talks to the `Agent`, and prints through `ctx` (lines of styled text) or
asks the screen to do something (clear, replay messages, start a turn, open a viewer). It never imports the screen.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...clara.types import Message
from ...core.agent import Agent
from ..base import Line


class SlashScreen(Protocol):
    """What a slash command may ask of the screen."""

    def clear_transcript(self) -> None: ...

    def show_answer(self, text: str) -> None:
        """An assistant message (Markdown), as if it had just been answered."""

    def replay(self, messages: Sequence[Message]) -> None:
        """Draw a restored conversation."""

    def start_turn(self, text: str, *, label: str = "", retry: bool = False) -> None:
        """Send `text` as a message. `label` is what the transcript shows instead of the text (for /init). `retry`: as a new version."""

    def view(self, title: str, lines: Sequence[Line]) -> None:
        """A full-screen viewer with search."""

    async def pick(self, title: str, rows: Sequence[tuple[str, str]], current: str = "") -> str | None:
        """A list to click or arrow through: `rows` are (value, label); returns the chosen value, None when cancelled."""

    def status_changed(self) -> None:
        """Something the status line shows has changed (the model, plan mode, the project...)."""

    def exit(self) -> None: ...


@dataclass
class SlashContext:
    agent: Agent
    screen: SlashScreen
    out: Callable[[Line], None]
    err: Callable[[str], None]
    name: str = ""

    def say(self, text: str = "", style: str = "") -> None:
        self.out(Line.of(text, style))

    def fail(self, message: str) -> None:
        self.err(f"/{self.name}: {message}")

    def table(self, rows: Iterable[Sequence[str]], styles: Sequence[str] = (), indent: int = 2) -> None:
        """Rows of cells, in aligned columns (the last column is not padded)."""
        grid = [list(row) for row in rows]
        if not grid:
            return
        widths = [max(len(row[i]) for row in grid if i < len(row)) for i in range(max(len(row) for row in grid))]
        for row in grid:
            pieces: list[tuple[str, str]] = [(" " * indent, "")]
            for index, cell in enumerate(row):
                last = index == len(row) - 1
                pieces.append((cell if last else cell.ljust(widths[index] + 2), styles[index] if index < len(styles) else ""))
            self.out(Line.join(*pieces))


Handler = Callable[[SlashContext, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SlashSpec:
    name: str
    summary: str
    usage: str
    run: Handler
    group: str = "Conversation"
    busy_ok: bool = False  # may run while an answer is being written
    aliases: tuple[str, ...] = ()


class SlashRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, SlashSpec] = {}

    def add(self, spec: SlashSpec) -> None:
        for name in (spec.name, *spec.aliases):
            self._specs[name] = spec

    def get(self, name: str) -> SlashSpec | None:
        return self._specs.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def specs(self) -> list[SlashSpec]:
        seen: dict[str, SlashSpec] = {}
        for spec in self._specs.values():
            seen[spec.name] = spec
        return sorted(seen.values(), key=lambda s: s.name)

    def pairs(self) -> list[tuple[str, str]]:
        """(name, one line of help) of every command: what the completion popup shows."""
        return [(spec.name, spec.summary) for spec in self.specs()]


def split_args(text: str) -> list[str]:
    """Words, with quotes. An unclosed quote is taken literally rather than refused."""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


def count(number: int) -> str:
    return f"{number:,}"
