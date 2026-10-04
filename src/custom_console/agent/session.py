"""Runs one agent turn: streams the answer into a TurnView and traces tool calls.

The model runs on the Clara server. The console sends it the user's message with its tools
(described, not sent as code); when the model calls one, the server asks the console to run it
here and the console answers with the result (see `remote.py`).
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .checkpoints import Checkpoints
from .clara import ClaraError
from .context import ContextManager, turn_notes
from .diffs import count_changes
from .journal import JsonlLogger
from .remote import RemoteAgent, run_remote_turn
from .results import ToolResult
from .tools.state import ReadTracker, TodoList
from .turn import DIFF, STYLE_ERROR, Segment, TurnStats, TurnView
from .usage import UsageLedger

DETAIL_MAX_LINES = 400  # of what a tool line hides
DETAIL_MAX_CHARS = 40_000
SUMMARY_MAX_CHARS = 50
PROGRESS_MAX_LINES = 200  # of what a running tool printed, kept under its line until it is done


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
    """Glue between the Clara server and the UI.

    `run_turn` is meant to run in a worker thread. The remote agent is bound after
    construction (`session.remote = ...`) because the tools it lends to the server are built
    from objects that themselves refer to this session.

    What the model is told with the next message besides it: `pending_notes` (a command the user
    ran, an undo...; told once), and what `attach(prompt)` gives (files the message mentions).
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
        location: Callable[[], str] = lambda: "",
    ):
        self._remote: Optional[RemoteAgent] = None
        self.location = location  # the agent's working directory, told with each message
        self.pending_notes: List[str] = []
        self.attach: Callable[[str], List[str]] = lambda prompt: []
        self.journal = journal
        self.user_id = user_id
        self.context = context
        self.usage = usage
        self.model = model
        self.provider = ""
        self.checkpoints = checkpoints
        self.reads = reads
        self.todos = todos
        self.clock = clock
        self.on_turn: Optional[Callable[[TurnView], None]] = None  # told of every finished turn (saved sessions)
        self.on_model: Optional[Callable[[str, str], None]] = None  # told when the server's model changes
        self._view: Optional[TurnView] = None
        self._cancel: Optional[threading.Event] = None
        self.current_tool: Optional[Segment] = None  # line of the tool running now (for sub-agents)

    @property
    def remote(self) -> RemoteAgent:
        if self._remote is None:
            raise RuntimeError("No remote agent bound to the session yet.")
        return self._remote

    @remote.setter
    def remote(self, remote: RemoteAgent) -> None:
        self._remote = remote

    @property
    def cancel_event(self) -> Optional[threading.Event]:
        """The event that stops the turn in progress (sub-agents watch it too)."""
        return self._cancel

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

    def tool_progress(self, line: str) -> None:
        """A line of what the running tool does (a command's output): shown under its line."""
        view, note = self._view, self.current_tool
        if view is not None and note is not None:
            view.add_detail(note, line, keep=PROGRESS_MAX_LINES)

    # -- tools ----------------------------------------------------------------- #

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Run the tool the server asked for, here, and give back what the model is told."""
        tools = {tool.__name__: tool for tool in self.remote.tools()}
        function = tools.get(name)
        if function is None:

            def function(**_: Any) -> ToolResult:  # shown and journaled like any failed call
                raise LookupError(f"{name} is not an available tool (it may have been turned off).")

        return self.tool_hook(name, function, arguments)

    def tool_hook(self, function_name: str, function_call: Callable[..., Any], arguments: Dict[str, Any]) -> str:
        """Show and journal a tool call, run it, and hand the model a compact text.

        It calls `function_call(**arguments)` itself, which lets it time the call and turn
        every outcome into a ToolResult.
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

    def instructions(self) -> str:
        """The agent's system prompt, plus what changes from one turn to the next."""
        extra = self.context.additional_context()
        base = self.remote.instructions()
        return f"{base}\n\n{extra}" if extra else base

    def request_body(self, prompt: str) -> Dict[str, Any]:
        """The request of a turn. The automatic notes (working directory, files that changed,
        attachments...) go in the `prefix`: the server shows them to the model with the message,
        keeps them in the history and leaves them out of summaries. The pending notes are told
        once: they are taken here."""
        stale = self.reads.stale() if self.reads is not None else []
        extra, self.pending_notes = [*self.pending_notes, *self.attach(prompt)], []
        return self.remote.client.body(
            prompt,
            self.context.session_id,
            tools=self.remote.schemas(),
            instructions=self.instructions(),
            prefix=turn_notes(stale, self.location(), extra, now=self.clock()),
        )

    def run_turn(self, view: TurnView, cancel: threading.Event) -> Optional[TurnStats]:
        """Stream the agent's answer to `view`. Never raises."""
        self._view, self._cancel = view, cancel
        self.journal.log_prompt(view.prompt)
        if self.checkpoints is not None:
            self.checkpoints.begin_turn(view.prompt[:60])
        started = time.perf_counter()
        done: Optional[Dict[str, Any]] = None
        try:
            done = run_remote_turn(
                self.remote.client,
                self.request_body(view.prompt),
                self.execute_tool,
                on_text=view.add_text,
                on_thinking=view.add_thinking,
                on_server_tool=view.server_tool,
                on_usage=lambda prompt_tokens, completion_tokens: view.request_finished(completion_tokens),
                on_note=view.add_note,
                cancel=cancel,
            )
        except ClaraError as error:
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
            self.journal.log_error(str(error))
        except Exception as error:  # the UI must survive anything
            view.add_note(f"Agent error: {type(error).__name__}: {error}", STYLE_ERROR)
            self.journal.log_error(f"{type(error).__name__}: {error}")
        finally:
            view.close_thinking()
            self._view = None  # `_cancel` stays until the next turn so that `cancelled` is readable

        if cancel.is_set():
            view.add_note("Interrupted by the user.")
        self.journal.log_answer(view.answer_text())

        stats = self._account(done, time.perf_counter() - started) if done is not None else None
        view.stats = stats
        if self.on_turn is not None:
            try:
                self.on_turn(view)
            except Exception as error:  # losing the saved history must not break the turn
                self.journal.log_error(f"session not saved: {error}")
        return stats

    def _account(self, done: Dict[str, Any], duration: float) -> TurnStats:
        """Usage ledger, context size and model after a turn."""
        usage = done.get("usage") or {}
        prompt_tokens, completion_tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        self.context.observe(done.get("context"))
        self._use_model(str(done.get("model") or self.model), str(done.get("provider") or self.provider))
        stats = TurnStats(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, duration)
        self.usage.record(self.model, stats)
        return dataclasses.replace(stats, context_percent=self.context.percent)

    def _use_model(self, model: str, provider: str) -> None:
        """The server may switch model or provider at any time (its /provider command)."""
        changed = (model, provider) != (self.model, self.provider)
        self.model, self.provider = model, provider
        if changed and self.on_model is not None:
            self.on_model(model, provider)

    # -- context -------------------------------------------------------------- #

    def refresh_context(self) -> Optional[Dict[str, Any]]:
        """Ask the server how full the conversation's context is (None if it cannot say)."""
        try:
            info = self.remote.client.context(self.context.session_id)
        except ClaraError:
            return None
        self.context.observe(info)
        self.context.summary = str(info.get("summary") or "")
        return info

    def compact(self, focus: str = "") -> Tuple[float, float]:
        """Replace the older messages by a summary, on the server. Returns the context usage
        (percent) before and after. Blocking: call it from a worker thread. Raises
        :class:`NothingToCompact` (a LookupError) when the conversation is empty."""
        result = self.remote.client.compact(self.context.session_id, focus)
        self.refresh_context()
        return float(result["before_percent"]), float(result["after_percent"])

    def clear(self, session_id: str) -> None:
        """Start a fresh conversation (``/clear``); the old one stays on the server."""
        self.context.reset(session_id)
        if self.todos is not None:
            self.todos.clear()
        if self.reads is not None:
            self.reads.clear()
