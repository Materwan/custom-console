"""Model of one agent turn: what was streamed, tool activity and statistics.

A :class:`TurnView` is filled by the agent's worker thread (text chunks, tool
calls, permission decisions) and read by the UI thread, hence the lock.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, List, Mapping, Optional

TEXT = "text"  # what the model says
NOTE = "note"  # one activity line (tool call, permission, error)
DIFF = "diff"  # the change a tool made to a file (saved sessions of older versions)
TODO = "todo"  # the agent's checklist (saved sessions of older versions)

# Note styles
STYLE_NOTE = "note"
STYLE_TOOL = "tool"
STYLE_PERMISSION = "permission"
STYLE_ERROR = "error"
STYLE_THINKING = "thinking"

THINKING_MARK = "✻"
SERVER_SUMMARY_CHARS = 50


@dataclass
class Segment:
    kind: str  # TEXT (model output) or NOTE (activity line)
    text: str
    style: str = ""
    detail: str = ""  # what a tool line hides until the details are shown (output, diff)
    detail_kind: str = ""  # DIFF when `detail` is a diff, "" for plain text
    done: bool = True  # False while the tool of this line runs: the line still changes


@dataclass(frozen=True)
class TurnStats:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration: float = 0.0
    context_percent: Optional[float] = None  # how full the context window is after the turn

    @property
    def tokens_per_second(self) -> float:
        return self.output_tokens / self.duration if self.duration > 0 else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TurnStats":
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})



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
        self.todos = ""  # the checklist as the agent last wrote it during this turn
        self.started = time.monotonic()
        self._segments: List[Segment] = []
        self._running_tools: List[Segment] = []
        self._thinking: Optional[Segment] = None  # the model's reasoning being streamed
        self._thinking_started = 0.0
        self._tokens_counted = 0  # output tokens of the finished model requests
        self._chunks = 0  # text chunks of the request in progress (about one token each)
        self._lock = threading.RLock()

    # -- writing (worker thread) ------------------------------------------- #

    def add_text(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self.close_thinking()
            self._chunks += 1
            if self._segments and self._segments[-1].kind == TEXT:
                self._segments[-1].text += chunk
            else:
                self._segments.append(Segment(TEXT, chunk))

    def add_thinking(self, chunk: str) -> None:
        """A piece of the model's reasoning: one line ("✻ thinking…") that hides the text."""
        if not chunk:
            return
        with self._lock:
            self._chunks += 1  # reasoning tokens are generated tokens
            if self._thinking is None:
                self._thinking = Segment(NOTE, f"{THINKING_MARK} thinking…", STYLE_THINKING, done=False)
                self._thinking_started = time.monotonic()
                self._segments.append(self._thinking)
            self._thinking.detail += chunk

    def close_thinking(self) -> None:
        """The reasoning in progress is over (the model writes, calls a tool, or the turn ends)."""
        with self._lock:
            note, self._thinking = self._thinking, None
            if note is not None:
                note.detail = note.detail.strip()
                note.text = f"{THINKING_MARK} thought for {time.monotonic() - self._thinking_started:.1f}s"
                note.done = True

    def server_tool(self, name: str, arguments: Mapping[str, Any], result: str) -> Segment:
        """A tool the server ran itself (remember, web_search...): one finished line, its result hidden."""
        failed = result.startswith("Error")
        lines = result.strip().split("\n") if result.strip() else []
        if len(lines) == 1 and len(lines[0]) <= SERVER_SUMMARY_CHARS:
            summary, detail = lines[0], ""
        else:
            summary, detail = (f"{len(lines)} lines" if lines else ""), result.strip()
        text = f"{'✘' if failed else '✔'} {name}({format_arguments(arguments)}) · on the server"
        if summary:
            text += f" · {summary}"
        with self._lock:
            self.close_thinking()
            note = Segment(NOTE, text, STYLE_ERROR if failed else STYLE_TOOL, detail)
            self._segments.append(note)
            return note

    def count_chunk(self) -> None:
        """A chunk of text was generated but not shown (a sub-agent's answer)."""
        with self._lock:
            self._chunks += 1

    def request_finished(self, output_tokens: Optional[int]) -> None:
        """A model request ended: its exact output count replaces the estimate."""
        with self._lock:
            self._tokens_counted += output_tokens if output_tokens else self._chunks
            self._chunks = 0

    def add_note(self, text: str, style: str = STYLE_NOTE) -> Segment:
        """Append an activity line; the returned handle can be passed to
        :meth:`update_note` (handles stay valid when other lines are inserted)."""
        note = Segment(NOTE, text, style)
        with self._lock:
            self.close_thinking()
            self._segments.append(note)
        return note

    def show_todos(self, checklist: str) -> None:
        """Remember the agent's checklist (shown apart from the stream; empty clears it)."""
        with self._lock:
            self.todos = checklist

    def update_note(self, note: Segment, text: str, style: Optional[str] = None) -> None:
        with self._lock:
            note.text = text
            if style is not None:
                note.style = style

    def add_detail(self, note: Segment, line: str, keep: Optional[int] = None) -> None:
        """Add a line to what a (running) tool line hides, e.g. a sub-agent's activity or a
        command's output; with `keep`, only the last `keep` lines stay."""
        with self._lock:
            note.detail = f"{note.detail}\n{line}" if note.detail else line
            if keep is not None and note.detail.count("\n") >= keep * 2:  # trimmed now and then, not at each line
                note.detail = "\n".join(note.detail.split("\n")[-keep:])

    def tool_started(self, name: str, arguments: Mapping[str, Any]) -> Segment:
        with self._lock:
            self.activity = f"running {name}"
            note = self.add_note(f"▸ {name}({format_arguments(arguments)}) …", STYLE_TOOL)
            note.done = False
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
        *,
        summary: str = "",
        detail: Optional[str] = None,
        detail_kind: str = "",
    ) -> None:
        """Turn the line of a running tool into its outcome. `summary` is a few words
        shown on the line (``+12 −3``); `detail` replaces what the line hides."""
        mark = "✔" if success else "✘"
        text = f"{mark} {name}({format_arguments(arguments)}) · {duration:.1f}s"
        if summary:
            text += f" · {summary}"
        if error:
            text += f" — {error}"
        with self._lock:
            self.activity = "thinking"
            self._running_tools = [n for n in self._running_tools if n is not note]
            self.update_note(note, text, STYLE_TOOL if success else STYLE_ERROR)
            if detail is not None:
                note.detail, note.detail_kind = detail, detail_kind
            note.done = True

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

    # -- saving and restoring ----------------------------------------------- #

    def to_dict(self) -> dict:
        """The finished turn as plain data (what ``/restore`` shows again)."""

        def segment(s: Segment) -> dict:
            data = {"kind": s.kind, "text": s.text, "style": s.style}
            if s.detail:
                data.update(detail=s.detail, detail_kind=s.detail_kind)
            return data

        with self._lock:
            data = {
                "prompt": self.prompt,
                "segments": [segment(s) for s in self._segments],
                "stats": self.stats.to_dict() if self.stats is not None else None,
            }
            if self.todos:
                data["todos"] = self.todos
            return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TurnView":
        view = cls(str(data.get("prompt", "")))
        view.todos = str(data.get("todos") or "")
        for item in data.get("segments", []):
            kind = str(item.get("kind", TEXT))
            if kind == TODO:  # older sessions kept the checklist among the segments
                view.todos = str(item.get("text", ""))
                continue
            view._segments.append(
                Segment(
                    kind,
                    str(item.get("text", "")),
                    str(item.get("style", "")),
                    str(item.get("detail", "")),
                    str(item.get("detail_kind", "")),
                )
            )
        if isinstance(data.get("stats"), Mapping):
            view.stats = TurnStats.from_dict(data["stats"])
        return view

    # -- reading (UI thread) ----------------------------------------------- #

    def snapshot(self) -> List[Segment]:
        """Independent copy of the segments (safe to iterate while streaming)."""
        with self._lock:
            return [dataclasses.replace(s) for s in self._segments]

    def answer_text(self) -> str:
        """All the model output, without the activity lines."""
        with self._lock:
            return "".join(s.text for s in self._segments if s.kind == TEXT)

    @property
    def output_tokens(self) -> int:
        """Tokens generated so far: exact for the finished requests, estimated for the current one."""
        with self._lock:
            return self._tokens_counted + self._chunks

    @property
    def elapsed(self) -> float:
        if self.stats is not None:
            return self.stats.duration
        return time.monotonic() - self.started
