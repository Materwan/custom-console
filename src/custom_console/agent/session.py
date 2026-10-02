"""Runs one agent turn: streams the answer into a TurnView and traces tool calls."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .checkpoints import Checkpoints
from .context import ContextManager, Summarizer, build_transcript, context_tokens_of, estimate_messages, turn_notes
from .diffs import count_changes
from .journal import JsonlLogger
from .results import ToolResult
from .tools.state import ReadTracker, TodoList
from .turn import DIFF, STYLE_ERROR, Segment, TurnStats, TurnView
from .usage import UsageLedger

DETAIL_MAX_LINES = 400  # of what a tool line hides
DETAIL_MAX_CHARS = 40_000
SUMMARY_MAX_CHARS = 50


def _data_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, dict) and isinstance(data.get("output"), str):  # run_command
        return data["output"]
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _clip_detail(text: str) -> str:
    text = text[:DETAIL_MAX_CHARS]
    lines = text.split("\n")
    if len(lines) > DETAIL_MAX_LINES:
        lines = [*lines[:DETAIL_MAX_LINES], f"… {len(lines) - DETAIL_MAX_LINES} more line(s)"]
    return "\n".join(lines)


def tool_display(result: ToolResult) -> Tuple[str, str, str]:
    """``(summary, detail, detail_kind)`` of a tool's outcome, for its line in the turn:
    a few words shown on the line, and what the line hides until unfolded."""
    if result.diff:
        data = result.data if isinstance(result.data, dict) else {}
        added, removed = count_changes(result.diff)
        added, removed = data.get("lines_added", added), data.get("lines_removed", removed)
        detail = result.detail if result.detail is not None else result.diff
        return result.summary or f"+{added} −{removed}", _clip_detail(detail), DIFF if result.detail is None else ""

    detail = _clip_detail(result.detail if result.detail is not None else _data_text(result.data)).strip("\n")
    if result.summary is not None or not result.success:
        return result.summary or "", detail, ""
    lines = detail.split("\n") if detail else []
    if len(lines) == 1 and len(lines[0]) <= SUMMARY_MAX_CHARS:
        return lines[0], "", ""  # the whole result fits on the line: nothing to hide
    return (f"{len(lines)} lines" if lines else ""), detail, ""


class AgentSession:
    """Glue between an agno agent and the UI.

    `run_turn` is meant to run in a worker thread. The agent is bound after
    construction (`session.agent = ...`) because the tools it needs are built
    from objects that themselves refer to this session.
    """

    def __init__(
        self,
        journal: JsonlLogger,
        user_id: str,
        context: ContextManager,
        usage: UsageLedger,
        *,
        model: str = "",
        checkpoints: Optional[Checkpoints] = None,
        reads: Optional[ReadTracker] = None,
        todos: Optional[TodoList] = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.agent: Any = None
        self.journal = journal
        self.user_id = user_id
        self.context = context
        self.usage = usage
        self.model = model
        self.checkpoints = checkpoints
        self.reads = reads
        self.todos = todos
        self.clock = clock
        self.summarize: Optional[Summarizer] = None  # set by the console: asks the model for a summary
        self.on_turn: Optional[Callable[[TurnView], None]] = None  # told of every finished turn (saved sessions)
        self._view: Optional[TurnView] = None
        self._cancel: Optional[threading.Event] = None
        self.current_tool: Optional[Segment] = None  # line of the tool running now (for sub-agents)

    @property
    def cancelled(self) -> bool:
        """True while the turn in progress was interrupted by the user."""
        return self._cancel is not None and self._cancel.is_set()

    @property
    def view(self) -> Optional[TurnView]:
        """The turn in progress, if any."""
        return self._view

    # -- permission decisions ------------------------------------------------ #

    def record_permission(self, info: str, status: str) -> None:
        if self._view is not None:
            self._view.permission(info, status)

    # -- tool hook ------------------------------------------------------------ #

    def tool_hook(self, function_name: str, function_call: Callable[..., Any], arguments: Dict[str, Any]) -> str:
        """agno tool hook: show and journal the call, hand the model a compact text.

        The hook is told by agno to call `function_call(**arguments)` itself, which
        lets it time the call and turn every outcome into a ToolResult.
        """
        view = self._view
        if self.cancelled:
            return ToolResult.fail(RuntimeError("Interrupted by the user.")).to_llm()

        note = view.tool_started(function_name, arguments) if view is not None else None
        self.current_tool = note
        started = time.monotonic()
        try:
            outcome = function_call(**arguments)
        except Exception as error:  # a tool must never break the agent loop
            outcome = ToolResult.fail(error)
        finally:
            self.current_tool = None
        duration = time.monotonic() - started

        result = outcome if isinstance(outcome, ToolResult) else ToolResult.ok(outcome)
        if view is not None and note is not None:
            summary, detail, detail_kind = tool_display(result)
            view.tool_finished(
                note,
                function_name,
                arguments,
                result.success,
                duration,
                None if result.success else str(result.error),
                summary=summary,
                detail=detail,
                detail_kind=detail_kind,
            )
            if result.todos is not None:
                view.show_todos(result.todos)
        self.journal.log_tool_call(
            function_name,
            arguments,
            result.to_dict(),
            duration,
            error=None if result.success else str(result.error),
        )
        return result.to_llm()

    # -- turn ----------------------------------------------------------------- #

    def model_input(self, prompt: str) -> str:
        """The user's message as the model gets it: after the date and time, and the
        list of the files that changed since the agent read them (see `turn_notes`)."""
        stale = self.reads.stale() if self.reads is not None else []
        return f"{turn_notes(self.clock(), stale)}\n\n{prompt}"

    def run_turn(self, view: TurnView, cancel: threading.Event) -> Optional[TurnStats]:
        """Stream the agent's answer to `view`. Never raises."""
        from agno.run.agent import RunEvent, RunOutput

        self._view, self._cancel = view, cancel
        self.journal.log_prompt(view.prompt)
        if self.checkpoints is not None:
            self.checkpoints.begin_turn(view.prompt[:60])
        self.agent.additional_context = self.context.additional_context() or None
        started = time.perf_counter()
        run_output = None
        try:
            for chunk in self.agent.run(
                self.model_input(view.prompt),
                stream=True,
                stream_events=True,  # for the token count of each model request
                yield_run_output=True,
                user_id=self.user_id,
                session_id=self.context.session_id,
            ):
                if cancel.is_set():
                    break
                if isinstance(chunk, RunOutput):
                    run_output = chunk
                    continue
                event = getattr(chunk, "event", None)
                if event == RunEvent.model_request_completed:
                    view.request_finished(getattr(chunk, "output_tokens", None))
                    continue
                if event is not None and event != RunEvent.run_content:
                    continue
                content = getattr(chunk, "content", None)
                if isinstance(content, str) and content:
                    view.add_text(content)
        except Exception as error:
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
            self.journal.log_error(str(error))
        finally:
            self._view = None  # `_cancel` stays until the next turn so that `cancelled` is readable

        if cancel.is_set():
            view.add_note("Interrupted by the user.")
        self.journal.log_answer(view.answer_text())

        stats = self._account(run_output, time.perf_counter() - started)
        if stats is not None and not cancel.is_set():
            self._compact_if_needed(view)
        view.stats = stats
        if self.on_turn is not None:
            try:
                self.on_turn(view)
            except Exception as error:  # losing the saved history must not break the turn
                self.journal.log_error(f"session not saved: {error}")
        return stats

    def _account(self, run_output: Any, duration: float) -> Optional[TurnStats]:
        """Usage ledger and context size after a turn."""
        metrics = getattr(run_output, "metrics", None)
        messages = getattr(run_output, "messages", None) or []
        if messages:
            self.context.observe(context_tokens_of(messages), estimate_messages(messages))
        if metrics is None:
            return None
        stats = TurnStats.from_metrics(metrics, duration)
        self.usage.record(self.model, stats)
        return dataclasses.replace(stats, context_percent=self.context.percent)

    # -- context -------------------------------------------------------------- #

    def _compact_if_needed(self, view: TurnView) -> None:
        if not self.context.should_compact():
            return
        view.activity = "compacting"
        view.add_note(f"Context is {self.context.percent:.0f}% full: compacting the conversation…")
        try:
            before, after = self.compact()
            view.add_note(f"Conversation compacted ({before:.0f}% → {after:.0f}% of the context).")
        except Exception as error:
            view.add_note(f"Could not compact the conversation: {error}", STYLE_ERROR)

    def compact(self, focus: str = "") -> Tuple[float, float]:
        """Replace the conversation by a summary. Returns the context usage
        (percent) before and after. Blocking: call it from a worker thread."""
        if self.summarize is None:
            raise RuntimeError("no summariser configured")
        messages: List[Any] = self.agent.get_chat_history(session_id=self.context.session_id) or []
        transcript = build_transcript(messages)
        if not transcript.strip() and not self.context.summary:
            raise LookupError("the conversation is empty: nothing to compact")
        summary = self.summarize(transcript, self.context.summary, focus)
        before = self.context.percent
        self.context.adopt_summary(summary)
        self.agent.additional_context = self.context.additional_context() or None
        return before, self.context.percent

    def clear(self, base_session_id: Optional[str] = None) -> None:
        """Start a fresh conversation (``/clear``), as a new session if an id is given."""
        self.context.reset(base_session_id)
        self.agent.additional_context = self.context.additional_context() or None
        if self.todos is not None:
            self.todos.clear()
        if self.reads is not None:
            self.reads.clear()
