"""Model of one agent turn: what was streamed, tool activity and statistics.

A :class:`TurnView` is filled by the agent's worker thread (text chunks, tool
calls, permission decisions) and read by the UI thread, hence the lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

TEXT = "text"
NOTE = "note"

# Note styles
STYLE_NOTE = "note"
STYLE_TOOL = "tool"
STYLE_PERMISSION = "permission"
STYLE_ERROR = "error"


@dataclass
class Segment:
    kind: str  # TEXT (model output) or NOTE (activity line)
    text: str
    style: str = ""


@dataclass(frozen=True)
class TurnStats:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.output_tokens / self.duration if self.duration > 0 else 0.0

    def __add__(self, other: "TurnStats") -> "TurnStats":
        return TurnStats(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.total_tokens + other.total_tokens,
            self.duration + other.duration,
        )

    @classmethod
    def from_metrics(cls, metrics: Any, duration: float) -> "TurnStats":
        """Build from an agno metrics object (missing/None fields count as 0)."""

        def number(name: str) -> int:
            return int(getattr(metrics, name, 0) or 0)

        return cls(
            input_tokens=number("input_tokens"),
            output_tokens=number("output_tokens"),
            total_tokens=number("total_tokens"),
            duration=duration,
        )


def _short(value: Any, limit: int) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_arguments(arguments: Mapping[str, Any], limit: int = 100) -> str:
    """``path='.', depth=2`` (shortened so a tool call stays on one line)."""
    text = ", ".join(f"{key}={_short(value, 40)}" for key, value in arguments.items())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TurnView:
    def __init__(self, prompt: str):
        self.prompt = prompt
        self.stats: Optional[TurnStats] = None
        self.activity = "thinking"
        self._segments: List[Segment] = []
        self._running_tools: List[Segment] = []
        self._lock = threading.RLock()

    # -- writing (worker thread) ------------------------------------------- #

    def add_text(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            if self._segments and self._segments[-1].kind == TEXT:
                self._segments[-1].text += chunk
            else:
                self._segments.append(Segment(TEXT, chunk))

    def add_note(self, text: str, style: str = STYLE_NOTE) -> Segment:
        """Append an activity line; the returned handle can be passed to
        :meth:`update_note` (handles stay valid when other lines are inserted)."""
        note = Segment(NOTE, text, style)
        with self._lock:
            self._segments.append(note)
        return note

    def update_note(self, note: Segment, text: str, style: Optional[str] = None) -> None:
        with self._lock:
            note.text = text
            if style is not None:
                note.style = style

    def tool_started(self, name: str, arguments: Mapping[str, Any]) -> Segment:
        with self._lock:
            self.activity = f"running {name}"
            note = self.add_note(f"▸ {name}({format_arguments(arguments)}) …", STYLE_TOOL)
            self._running_tools.append(note)
            return note

    def tool_finished(
        self,
        note: Segment,
        name: str,
        arguments: Mapping[str, Any],
        success: bool,
        duration: float,
        error: Optional[str] = None,
    ) -> None:
        mark = "✔" if success else "✘"
        text = f"{mark} {name}({format_arguments(arguments)}) · {duration:.1f}s"
        if error:
            text += f" — {error}"
        with self._lock:
            self.activity = "thinking"
            self._running_tools = [n for n in self._running_tools if n is not note]
            self.update_note(note, text, STYLE_TOOL if success else STYLE_ERROR)

    def permission(self, info: str, status: str) -> None:
        """Record a permission decision.

        It is shown *before* the tool call it authorizes: the tool's line is
        created when the call starts, but the question is asked during it.
        """
        note = Segment(NOTE, f"? {info} → {status}", STYLE_PERMISSION)
        with self._lock:
            if self._running_tools:
                running = self._running_tools[-1]
                position = next(i for i, s in enumerate(self._segments) if s is running)
                self._segments.insert(position, note)
            else:
                self._segments.append(note)

    # -- reading (UI thread) ----------------------------------------------- #

    def snapshot(self) -> List[Segment]:
        """Independent copy of the segments (safe to iterate while streaming)."""
        with self._lock:
            return [Segment(s.kind, s.text, s.style) for s in self._segments]

    def answer_text(self) -> str:
        """All the model output, without the activity lines."""
        with self._lock:
            return "".join(s.text for s in self._segments if s.kind == TEXT)
