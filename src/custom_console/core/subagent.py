"""`local_task`: a sub-agent in its own incognito Clara conversation.

It has its own instructions and only the local tools that read, plus Clara's server tools. It cannot start another
sub-agent, has a round limit and a time limit, and gives back one text, cut to a fixed size. Prompts it raises (a read
outside the trusted folder, one of Clara's approvals) go to the person like any other, marked as the sub-agent's.

What the sub-agent read from outside taints the main turn too: its answer is text the main agent will act on, so a web
page the sub-agent fetched must not become a way to make the main agent write a file without asking.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable
from typing import Any, Protocol

from ..clara import ClaraClient
from ..clara.events import Event, ToolCall, ToolRequests
from ..platform import CancelToken
from .journal import Journal
from .ledger import Ledger
from .permissions.engine import PermissionEngine
from .prompt import SUBAGENT_PROMPT
from .session import Session
from .tools.base import Question, TodoStore, ToolContext, cut_middle
from .tools.registry import ToolRegistry
from .turn import Prompt, TurnRunner, TurnUI
from .vfs.guard import OverwriteGuard

log = logging.getLogger("custom_console.subagent")

ROUND_LIMIT = 12  # rounds of local tool calls
TIME_LIMIT = 300.0  # seconds
RESULT_LIMIT = 8_000  # characters given back


class SubagentView(Protocol):
    """What the interface shows of a sub-agent: a tab of its own that prints what it does, and a status line in the main one."""

    def sub_started(self, label: str, prompt: str, round_limit: int) -> int: ...

    def sub_event(self, number: int, event: Event) -> None: ...

    def sub_tool(self, number: int, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None: ...

    def sub_waiting(self, number: int, waiting: bool) -> None: ...

    def sub_finished(self, number: int, ok: bool, summary: str) -> None: ...


class _SubUI:
    """The `TurnUI` of the sub-agent's turn: counts rounds, forwards prompts to the person, shows what it does in its tab."""

    def __init__(self, parent: TurnUI, view: SubagentView, number: int, on_round: Callable[[], None]):
        self.parent, self.view, self.number, self.on_round = parent, view, number, on_round

    def show(self, event: Event) -> None:
        if isinstance(event, ToolRequests):
            self.on_round()
        self.view.sub_event(self.number, event)

    async def confirm(self, prompt: Prompt) -> str:
        self.view.sub_waiting(self.number, True)
        try:
            return await self.parent.confirm(dataclasses.replace(prompt, summary=f"[sub-agent] {prompt.summary}"))
        finally:
            self.view.sub_waiting(self.number, False)

    async def ask_user(self, question: Question) -> str:
        return ""  # a sub-agent cannot ask the person

    def approval_closed(self, approval_id: int, note: str) -> None:
        self.parent.approval_closed(approval_id, note)

    def tool_finished(self, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None:
        self.view.sub_tool(self.number, call, ok, result, seconds, preview)

    def note(self, text: str) -> None:
        self.parent.note(text)


class SubagentRunner:
    def __init__(self, *, client: ClaraClient, parent: Session, registry: ToolRegistry, engine: PermissionEngine,
                 ctx: ToolContext, ui: TurnUI, view: SubagentView, journal: Journal, ledger: Ledger,
                 parent_runner: Callable[[], TurnRunner | None], round_limit: int = ROUND_LIMIT,
                 time_limit: float = TIME_LIMIT):
        self.client, self.parent, self.registry, self.engine, self.ctx = client, parent, registry, engine, ctx
        self.ui, self.view, self.journal, self.ledger = ui, view, journal, ledger
        self.parent_runner = parent_runner
        self.round_limit, self.time_limit = round_limit, time_limit
        self._stops: dict[int, Callable[[], None]] = {}  # the sub-agents at work, by number

    def instructions(self) -> str:
        return f"{SUBAGENT_PROMPT.strip()}\n\n## This session\n- Working folder: {self.ctx.folder.virtual}"

    async def __call__(self, prompt: str, parent_cancel: CancelToken) -> str:
        label = " ".join(prompt.split())[:70]
        number = self.view.sub_started(label, prompt, self.round_limit)
        cancel = CancelToken()
        ctx = dataclasses.replace(self.ctx, cancel=cancel, vfs=self.ctx.vfs.fork(cancel), subagent=None, ask=None,
                                  todos=TodoStore(), extras={}, round_deadline=None,
                                  guard=OverwriteGuard(self.ctx.floor.case_insensitive))  # what it read unlocks nothing for the main agent
        session = Session(folder=self.parent.folder, incognito=True, local_only=self.parent.local_only,
                          model=self.parent.model, disabled_tools=set(self.parent.disabled_tools),
                          parent=self.parent.conversation)
        rounds = 0
        stopped_by = ""

        def on_round() -> None:
            nonlocal rounds, stopped_by
            rounds += 1
            if rounds > self.round_limit and not stopped_by:
                stopped_by = f"the round limit ({self.round_limit})"
                runner.cancel()

        runner = TurnRunner(client=self.client, session=session, registry=self.registry, engine=self.engine, ctx=ctx,
                            ui=_SubUI(self.ui, self.view, number, on_round), journal=self.journal, ledger=self.ledger,
                            instructions=self.instructions, store=None, agent="sub", subagent=True)

        def stop() -> None:
            nonlocal stopped_by
            stopped_by = stopped_by or "the person's request"
            runner.cancel()

        self._stops[number] = stop
        task = asyncio.create_task(runner.run(prompt))
        try:
            await asyncio.wait({task}, timeout=self.time_limit)
        except asyncio.CancelledError:  # the main turn was cancelled: stop the sub-agent with it
            runner.cancel()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            self.view.sub_finished(number, False, "stopped")
            raise
        finally:
            self._stops.pop(number, None)
        if not task.done():
            stopped_by = stopped_by or f"the time limit ({self.time_limit:.0f} s)"
            runner.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._carry_taint(runner)
        return self._answer(number, task, runner, stopped_by)

    def stop(self, number: int) -> bool:
        """Stop one sub-agent (the person pressed Esc in its tab). False when it is not at work."""
        stop = self._stops.get(number)
        if stop is not None:
            stop()
        return stop is not None

    def _carry_taint(self, runner: TurnRunner) -> None:
        main = self.parent_runner()
        if main is not None and runner.tainted:
            main.tainted = True

    def _answer(self, number: int, task: asyncio.Task[Any], runner: TurnRunner, stopped_by: str) -> str:
        if stopped_by:
            self.view.sub_finished(number, False, f"stopped: {stopped_by}")
            return f"The sub-agent was stopped at {stopped_by} and gave no answer. Do the rest yourself, or ask for less."
        if task.cancelled():
            self.view.sub_finished(number, False, "stopped")
            return "The sub-agent was stopped."
        error = task.exception()
        if error is not None:
            log.warning("sub-agent failed: %s", error)
            self.view.sub_finished(number, False, "failed")
            return f"The sub-agent failed ({type(error).__name__}). Do the job yourself."
        result = task.result()
        if result.error is not None:
            self.view.sub_finished(number, False, result.error.message)
            return f"The sub-agent failed: {result.error.message}"
        reply = result.reply.strip() or "(the sub-agent gave no answer)"
        self.view.sub_finished(number, True, f"{result.tools_run} tool calls")
        return cut_middle(reply, RESULT_LIMIT)
