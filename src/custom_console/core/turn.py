"""One turn, from the message to the last event.

The runner posts the message to Clara, reads the events, and does what each one asks:

* `token`, `thinking`, `retrying`, ... go to the interface to be drawn;
* an `approval` opens a prompt whose answer goes to `/v1/approvals/{id}/decide` (while the stream keeps being read, so an
  answer given on another client closes the prompt);
* `tool_requests` lists local tool calls. Each goes through the permission engine, is asked about if needed, is run, and the
  results of the whole round are posted together on the same stream;
* `usage` is written to the ledger the moment it arrives, so a turn that later fails or is cancelled still counts;
* an `error` event ends the turn with a readable message.

Cancelling the task that runs a turn closes the connection at any point (Clara then stores the turn as interrupted), and
sets the cancel token so a tool running in a worker thread stops too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..clara import ClaraClient, ClaraError
from ..clara.events import (
    ApprovalRequested,
    Done,
    ErrorEvent,
    Event,
    ToolCall,
    ToolDone,
    ToolRequests,
    ToolStart,
    TurnStarted,
    Usage,
)
from ..clara.types import Approval, Attachment, ChatBody
from ..platform import Cancelled, CancelToken
from .journal import Journal
from .ledger import Ledger
from .permissions.engine import ASK, REFUSE, CallInfo, Decision, PermissionEngine, Situation
from .permissions.propose import Asker, Proposal, plan
from .permissions.redact import hide_clara_tokens
from .session import Session, SessionStore, timezone_name
from .tools.base import Question, ToolContext, ToolError, call_handler, cut_middle, validate_args
from .tools.registry import ToolRegistry

log = logging.getLogger("custom_console.turn")

ROUND_BUDGET = 840.0  # seconds to answer a round of local tool calls (Clara waits 900)
REFUSED_TEXT = "The person refused this (request NOT done). Do not try another way round it."
NO_ANSWER_TEXT = "The person did not answer in time, so this was NOT done."

# Server tools that read nothing from outside. Any other server tool, including one this list does not know, counts as
# outside content: web, GitHub, Drive, project files, conversation files. After one, every local write and command asks.
SAFE_SERVER_TOOLS = frozenset({
    "add_task", "update_task", "delete_task", "list_tasks", "remind", "cancel_reminder", "list_reminders", "notify", "remember",
    "recall_facts", "forget", "search_history", "day_conversations", "day_activity", "read_conversation", "qcm", "ask_person",
    "finish_run", "ws_list", "ws_read", "ws_search", "ws_log", "ws_write", "ws_edit", "ws_move", "ws_delete", "ws_run",
    "open_second_brain", "vault_list", "vault_read", "vault_search", "vault_write", "vault_edit", "vault_move", "vault_daily",
    "vault_daily_block", "daily_notes",
})


@dataclass(frozen=True, slots=True)
class Prompt:
    """What the person is asked: the same for a local tool and for one of Clara's own approvals."""

    kind: str  # "local" or "server"
    tool: str
    summary: str
    reason: str = ""
    command: str = ""
    paths: tuple[Any, ...] = ()  # ShownPath
    preview: str = ""  # a diff, or the details
    can_remember: bool = False
    why_not_remembered: str = ""
    seconds_left: float | None = None
    approval_id: int | None = None
    undoable: bool = False
    level: str = ""
    floor: tuple[str, ...] = ()
    tainted: bool = False
    proposal: Proposal | None = None  # the patterns "always" could keep for a command (a key press asks the model)


class TurnUI(Protocol):
    """What the interface provides to a turn. Fakes in the tests; the Textual app in the program."""

    def show(self, event: Event) -> None: ...

    async def confirm(self, prompt: Prompt) -> str:
        """One of allow_once, allow_always, refuse_once, refuse_always. May be cancelled (the prompt must close)."""

    async def ask_user(self, question: Question) -> str: ...

    def approval_closed(self, approval_id: int, note: str) -> None: ...

    def tool_finished(self, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None: ...

    def note(self, text: str) -> None: ...


@dataclass
class TurnResult:
    outcome: str = "failed"  # ok, failed or cancelled
    reply: str = ""
    done: Done | None = None
    error: ClaraError | None = None
    tools_run: int = 0
    tool_names: list[str] = field(default_factory=list)


class TurnRunner:
    def __init__(self, *, client: ClaraClient, session: Session, registry: ToolRegistry, engine: PermissionEngine,
                 ctx: ToolContext, ui: TurnUI, journal: Journal, ledger: Ledger, instructions: Callable[[], str],
                 store: SessionStore | None = None, agent: str = "main", subagent: bool = False,
                 clock: Callable[[], float] = time.monotonic, proposer: Asker | None = None):
        self.client, self.session, self.registry, self.engine = client, session, registry, engine
        self.ctx, self.ui, self.journal, self.ledger = ctx, ui, journal, ledger
        self.instructions, self.store, self.agent, self.subagent = instructions, store, agent, subagent
        self._clock = clock
        self.proposer = proposer
        self.tainted = False
        self.turn_id = ""
        self.model = ""
        self._approval_tasks: dict[int, asyncio.Task[None]] = {}
        self._task: asyncio.Task[Any] | None = None
        self._offered: set[str] = set()

    # ------------------------------------------------------------------ control
    def cancel(self) -> None:
        """Stop the turn now: the tools, and the connection. Safe to call at any time."""
        self.ctx.cancel.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _begin(self) -> None:
        token = CancelToken()
        self.ctx.cancel = token
        self.ctx.vfs.cancel = token
        self.ctx.round_deadline = None
        self._task = asyncio.current_task()

    # ------------------------------------------------------------------ the turn
    def build_body(self, text: str, attachments: Sequence[Attachment], fork_of: int | None) -> ChatBody:
        session = self.session
        new = session.conversation is None
        specs = self.registry.offered(self.ctx, disabled=frozenset(session.disabled_tools), plan_mode=session.plan_mode,
                                      subagent=self.subagent)
        self._offered = {s.name for s in specs}
        notes = "\n\n".join(session.pending_notes)
        message = f"{notes}\n\n{text}" if notes and text else (notes or text)
        return ChatBody(
            message=message, conversation=session.conversation, attachments=tuple(attachments),
            project=session.project if new else None, model=session.model, fork_of=fork_of, timezone=timezone_name(),
            tools=tuple(s.schema() for s in specs) if fork_of is None else (),  # Clara refuses tools with fork_of
            instructions=self.instructions(), incognito=session.incognito if new else None,
            local_only=session.local_only if new else None, parent=session.parent if new else None)

    async def run(self, text: str, *, attachments: Sequence[Attachment] = (), fork_of: int | None = None,
                  tainted: bool = False) -> TurnResult:
        self._begin()
        result = TurnResult()
        started = self._clock()
        self.tainted = bool(attachments) or tainted  # an attachment, or text from a command, counts as outside content
        body = self.build_body(text, attachments, fork_of)
        first_event = True
        stream = self.client.chat_stream(body)
        try:
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if first_event:
                        first_event = False
                        self.session.pending_notes.clear()  # the request got through: the notes were used (BUG-7)
                    await self._handle(event, result)
            result.outcome = "failed" if result.error else "ok"
        except asyncio.CancelledError:
            result.outcome = "cancelled"
            raise
        except ClaraError as error:
            result.error = error
            result.outcome = "failed"
            self.journal.error(error.code, error.message)
        finally:
            for task in list(self._approval_tasks.values()):
                task.cancel()
            self._approval_tasks.clear()
            self.ledger.end_turn(self.session.id, result.outcome, agent=self.agent)
            self.journal.turn(self.turn_id, self.session.conversation, self.model, result.outcome,
                              self._clock() - started, len(body.message))
            self._save()
        return result

    def _save(self) -> None:
        if self.store is not None and not self.subagent:
            try:
                self.store.save(self.session)
            except OSError as error:
                log.warning("could not save the session: %s", error.strerror or error)

    # ------------------------------------------------------------------ events
    async def _handle(self, event: Event, result: TurnResult) -> None:
        self.ui.show(event)
        session = self.session
        if isinstance(event, TurnStarted):
            self.turn_id, self.model = event.id, event.model
            if event.conversation and session.conversation is None:
                session.conversation = event.conversation
            session.incognito, session.local_only = event.incognito, event.local_only
        elif isinstance(event, ToolStart):
            if event.name not in SAFE_SERVER_TOOLS:
                self.tainted = True
        elif isinstance(event, ToolDone):
            if event.name not in SAFE_SERVER_TOOLS:
                self.tainted = True
            if event.approval is not None:
                self._close_approval(event.approval, "Answered elsewhere, or no longer needed.")
        elif isinstance(event, ApprovalRequested):
            self._open_approval(event.approval)
        elif isinstance(event, ToolRequests):
            await self._tool_round(event, result)
        elif isinstance(event, Usage):
            session.prompt_tokens += event.prompt_tokens
            session.completion_tokens += event.completion_tokens
            self.ledger.record(session.id, prompt=event.prompt_tokens, completion=event.completion_tokens,
                               cached=event.cached_tokens, model=self.model, agent=self.agent,
                               conversation=session.conversation)
        elif isinstance(event, Done):
            result.done, result.reply = event, event.reply
            if event.conversation and session.conversation is None:
                session.conversation = event.conversation
            if event.message_id is not None:
                session.last_message_id = event.message_id
            session.context_percent = event.context_percent
            session.context_tokens, session.context_window = event.context_tokens, event.context_window
            if event.model:
                self.model = event.model
        elif isinstance(event, ErrorEvent):
            result.error = event.as_error()
            self.journal.error(event.code, event.message)

    # ------------------------------------------------------------------ Clara's own approvals
    def _open_approval(self, approval: Approval) -> None:
        if approval.id in self._approval_tasks:
            return
        self._approval_tasks[approval.id] = asyncio.create_task(self._answer_approval(approval))

    def _close_approval(self, approval_id: int, note: str) -> None:
        task = self._approval_tasks.pop(approval_id, None)
        if task is not None and not task.done():
            task.cancel()
            self.ui.approval_closed(approval_id, note)

    async def _answer_approval(self, approval: Approval) -> None:
        prompt = Prompt("server", approval.tool, approval.summary or approval.tool, approval.reason, preview=approval.target,
                        can_remember=True, approval_id=approval.id, level=approval.level)
        try:
            answer = await self.ui.confirm(prompt)
        except asyncio.CancelledError:
            raise
        self._approval_tasks.pop(approval.id, None)
        try:
            await self.client.decide(approval.id, answer)
            self.journal.approval(approval.id, approval.tool, answer, "console")
        except ClaraError as error:
            if error.code == "conflict":
                self.ui.note(f"Request #{approval.id} was already answered elsewhere.")
            else:
                self.ui.note(f"Could not send the answer for request #{approval.id}: {error}")

    # ------------------------------------------------------------------ local tools
    async def _tool_round(self, request: ToolRequests, result: TurnResult) -> None:
        deadline = self._clock() + ROUND_BUDGET
        self.ctx.round_deadline = time.monotonic() + ROUND_BUDGET
        answers: dict[str, str] = {}
        for call in request.calls:
            left = deadline - self._clock()
            if left <= 0:
                answers[call.id] = NO_ANSWER_TEXT
                continue
            answers[call.id] = await self.execute_call(call, left, result)
        self.ctx.round_deadline = None
        await self.client.send_tool_results(request.turn, answers)

    def situation(self) -> Situation:
        return Situation(self.ctx.folder, self.ctx.trusted, self.tainted, self.session.plan_mode)

    async def execute_call(self, call: ToolCall, left: float, result: TurnResult | None = None) -> str:
        """Decide, ask if needed, run, and shape the result of one local tool call. Never raises except to cancel."""
        started = self._clock()
        name, args = call.name, call.arguments
        spec = self.registry.get(name)
        if spec is None or name not in self._offered:
            return f"{name} is not available in this session."
        problems = validate_args(spec.parameters, args)
        if problems:
            return "Invalid arguments: " + "; ".join(problems) + "."
        try:
            info = spec.inspect(self.ctx, args)
        except ToolError as error:
            return str(error)
        except Exception as error:
            log.exception("inspecting %s failed", name)
            return f"The tool failed ({type(error).__name__}: {str(error)[:200]})."
        decision = self.engine.decide(info, self.ctx.vfs, self.situation())
        answer = ""
        if decision.effect == REFUSE:
            return self._finish(call, False, f"Refused: {decision.reason}", started, decision, "", "")
        preview = ""
        if decision.effect == ASK:
            preview = self._preview(spec.preview, args)
            prompt = self._prompt(info, decision, preview, left)
            try:
                answer = await asyncio.wait_for(self.ui.confirm(prompt), timeout=max(left - 5.0, 1.0))
            except TimeoutError:
                return self._finish(call, False, NO_ANSWER_TEXT, started, decision, "timeout", preview)
            line = self.engine.record(decision, answer, prompt.proposal.patterns if prompt.proposal else ())
            if line:
                self.ui.note(line)
            if answer.startswith("refuse"):
                return self._finish(call, False, REFUSED_TEXT, started, decision, answer, preview)
        try:
            text = await call_handler(spec, self.ctx, args)
            ok = True
        except ToolError as error:
            text, ok = str(error), False
        except Cancelled:
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("tool %s failed", name)
            text, ok = f"The tool failed ({type(error).__name__}: {str(error)[:200]}).", False
        if result is not None:
            result.tools_run += 1
            result.tool_names.append(name)
        return self._finish(call, ok, text, started, decision, answer or "allowed", preview)

    def _finish(self, call: ToolCall, ok: bool, text: str, started: float, decision: Decision, answer: str,
                preview: str) -> str:
        shaped = hide_clara_tokens(cut_middle(text, self.ctx.result_limit))
        seconds = self._clock() - started
        self.ui.tool_finished(call, ok, shaped, seconds, preview)
        self.journal.tool(call.name, call.arguments, decision.effect, decision.step, decision.reason, answer, ok, seconds,
                          len(shaped))
        return shaped

    def _preview(self, previewer: Callable[[ToolContext, dict[str, Any]], str] | None, args: dict[str, Any]) -> str:
        """The details the ask prompt shows. Built only when a prompt is shown (PERF-3), and never allowed to break the call."""
        if previewer is None:
            return ""
        try:
            return previewer(self.ctx, args)
        except Exception:
            return ""

    def _prompt(self, info: CallInfo, decision: Decision, preview: str, left: float) -> Prompt:
        proposal = plan(decision.parts, self.engine.windows, self.proposer) if decision.parts else None
        return Prompt("local", info.tool, info.summary, decision.reason, decision.command, decision.paths, preview,
                      decision.can_remember, decision.why_not_remembered, left, undoable=info.undoable, floor=decision.floor,
                      tainted=decision.tainted, proposal=proposal)

