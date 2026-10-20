"""One transcript: the main agent's, or a sub-agent's tab. It owns the answer and the thinking being written and the tools still
at work, so two agents never write into the same block."""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll
from textual.widget import Widget

from ...clara.events import (
    Compacted,
    Discarded,
    Done,
    Event,
    FileNote,
    Quiz,
    Retrying,
    Thinking,
    Token,
    ToolCall,
    ToolDone,
    ToolRequests,
    ToolStart,
    Warning,
)
from .blocks import AnswerBlock, NoteBlock, PendingTool, ThinkingBlock, tool_block


class Pane(VerticalScroll):
    def __init__(self, *children: Widget, local_pending: bool = False, **kwargs: Any):
        super().__init__(*children, **kwargs)
        self._answer: AnswerBlock | None = None
        self._thinking: ThinkingBlock | None = None
        self._pending: list[PendingTool] = []  # Clara's tools that started and did not end
        self._local: dict[str, PendingTool] = {}  # the console's own tools being run (drawn only when `local_pending`)
        self._local_pending = local_pending
        self._streaming = False
        self._early: list[Widget] = []  # what was added before the pane was on screen

    def on_mount(self) -> None:
        self.anchor()
        early, self._early = self._early, []
        for widget in early:
            self.mount(widget)

    def add(self, widget: Widget) -> None:
        if self.is_attached:
            self.mount(widget)
        else:
            self._early.append(widget)

    def say(self, text: str, level: str = "dim") -> None:
        self.add(NoteBlock(text, level))

    # ------------------------------------------------------------------ the blocks being written
    def answer(self) -> AnswerBlock:
        if self._answer is None:
            self._answer = AnswerBlock()
            self.add(self._answer)
        return self._answer

    def thinking(self) -> ThinkingBlock:
        if self._thinking is None:
            self._thinking = ThinkingBlock()
            self.add(self._thinking)
        return self._thinking

    def close_blocks(self) -> None:
        """The text and the thinking written so far are complete: draw them for good."""
        if self._thinking is not None:
            self._thinking.finish()
            self._thinking = None
        if self._answer is not None:
            self._answer.flush()
            self._answer.finish()
            self._answer = None

    def flush(self) -> None:
        for block in (self._thinking, self._answer):
            if block is not None:
                block.flush()

    def drop_pending(self) -> None:
        """A tool whose end never came (the turn was stopped)."""
        for stale in (*self._pending, *self._local.values()):
            stale.remove()
        self._pending.clear()
        self._local.clear()

    def reset(self) -> None:
        self._answer = self._thinking = None
        self._pending.clear()
        self._local.clear()

    # ------------------------------------------------------------------ what the agent does
    def handle_event(self, event: Event) -> None:
        if isinstance(event, Thinking):
            self.thinking().feed(event.text)
        elif isinstance(event, Token):
            if self._thinking is not None:
                self._thinking.finish()
                self._thinking = None
            self.answer().feed(event.text)
            self._streaming = True
        elif isinstance(event, Retrying):
            self.say(event.message or f"Trying again with {event.model}.", "warn")
        elif isinstance(event, ToolStart):
            self.close_blocks()
            pending = PendingTool(event.name, event.arguments)
            self._pending.append(pending)
            self.add(pending)
        elif isinstance(event, ToolDone):
            self._server_tool_done(event)
        elif isinstance(event, ToolRequests):
            self.close_blocks()
            if self._local_pending:
                for call in event.calls:
                    self._local[call.id] = pending = PendingTool(call.name.removeprefix("local_"), call.arguments)
                    self.add(pending)
        elif isinstance(event, Compacted):
            self.say(f"Compacted: {event.before:.0f}% to {event.after:.0f}% of the context.", "ok")
        elif isinstance(event, Discarded):
            self.say("Dropped unanswered tool calls of an earlier turn: " + ", ".join(event.tools), "dim")
        elif isinstance(event, Warning):
            self.say(event.message, "warn")
        elif isinstance(event, Quiz):
            self.say(f"Clara showed a quiz ({event.title or 'untitled'}); the console cannot display it.", "warn")
        elif isinstance(event, FileNote):
            self.say(f"Clara {event.action or 'wrote'} {event.path}", "dim")
        elif isinstance(event, Done):
            if self._answer is None and event.reply.strip() and not self._streaming:
                self.answer().feed(event.reply)
            self.close_blocks()
            self._streaming = False

    def _server_tool_done(self, event: ToolDone) -> None:
        for pending in self._pending:
            if pending.tool == event.name:
                self._pending.remove(pending)
                pending.remove()
                break
        self.add(tool_block(event.name, event.arguments, True, event.result, 0.0, server=True))

    def tool_finished(self, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None:
        if (pending := self._local.pop(call.id, None)) is not None:
            pending.remove()
        self.add(tool_block(call.name, call.arguments, ok, result, seconds, preview))
