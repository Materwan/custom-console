"""The status line: folder, conversation, model, badges, context fill, tokens, jobs, and the spinner while an answer is written."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"


@dataclass(slots=True)
class StatusFacts:
    folder: str
    title: str
    model: str
    incognito: bool
    local_only: bool
    plan: bool
    trusted: bool
    context_percent: float
    prompt_tokens: int
    completion_tokens: int
    jobs: int
    working_seconds: float | None = None  # None: idle
    frame: int = 0
    unicode: bool = True
    approvals: int = 0  # Clara's requests waiting for an answer somewhere else


def shorten(path: str, width: int = 38) -> str:
    return path if len(path) <= width else "..." + path[-(width - 3):]


def compact(number: int) -> str:
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)


def status_text(facts: StatusFacts) -> Text:
    """Yellow is for caution only (untrusted). The working indicator comes first, so a narrow terminal cuts the rest, not it."""
    line = Text(no_wrap=True, overflow="ellipsis")
    if facts.working_seconds is not None:
        frames = SPINNER_UNICODE if facts.unicode else SPINNER_ASCII
        line.append(f"{frames[facts.frame % len(frames)]} working {facts.working_seconds:.0f}s  (Esc stops)  ", style="bold")
    line.append(shorten(facts.folder), style="bold")
    line.append("  " + (facts.title or "new conversation"), style="" if facts.title else "dim")
    line.append("  " + (facts.model or "default model"), style="cyan")
    for on, label, style in ((facts.incognito, "incognito", "magenta"), (facts.local_only, "local only", "green"),
                             (facts.plan, "plan", "blue"), (not facts.trusted, "untrusted", "yellow")):
        if on:
            line.append(f" [{label}]", style=style)
    if facts.context_percent:
        style = "green" if facts.context_percent < 60 else "yellow" if facts.context_percent < 85 else "red"
        line.append(f"  ctx {facts.context_percent:.0f}%", style=style)
    if facts.prompt_tokens or facts.completion_tokens:
        arrows = ("↑", "↓") if facts.unicode else ("in ", "out ")
        line.append(f"  {arrows[0]}{compact(facts.prompt_tokens)} {arrows[1]}{compact(facts.completion_tokens)}", style="dim")
    if facts.jobs:
        line.append(f"  {facts.jobs} job{'s' if facts.jobs != 1 else ''}", style="bold")
    return line


class StatusLine(Static):
    def __init__(self) -> None:
        super().__init__("", id="status")

    def show(self, facts: StatusFacts) -> None:
        self.update(status_text(facts))
