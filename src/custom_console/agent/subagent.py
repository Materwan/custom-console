"""Sub-agents: what the `task` tool runs.

A sub-agent is a one-shot job given to the Clara server: an *ephemeral* turn on the same model.
It does not see the conversation (nor Clara's memory): it gets the job's prompt, works with its
own tools (the read-only ones unless writes are allowed) and hands back its final answer only,
which keeps the main conversation's context small.

It runs inside the `task` call, in the agent's worker thread. Its tool calls are listed under
the `task` line of the turn, its tokens count in the turn's total, and the permission questions
of its tools are asked as usual.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .clara import ClaraError
from .context import describe_now
from .permissions import PermissionLevel
from .remote import run_remote_turn
from .results import ToolResult
from .session import AgentSession, tool_display
from .turn import TurnStats, format_arguments

Tool = Callable[..., Any]

# Tools a sub-agent never gets: they belong to the conversation with the user.
EXCLUDED_TOOLS = frozenset({"task", "ask_user", "todo_write", "file_system_cd"})

INSTRUCTIONS = """\
You are a sub-agent: another agent handed you one job. Do it with your tools, then answer
with a report for that agent, who sees nothing but your final answer.
- Explore before concluding: search, then read. Never guess what a file contains.
- Report facts: what you found, with file paths (and line numbers when useful), and what
  remains uncertain. Be concise; do not copy whole files.
- Each tool returns JSON: {"success": true, "data": ...} or {"success": false, "error": ...}.
  When a tool fails, read the error and fix the call instead of repeating it.
- Answer in the language of the job.
"""


def subagent_tools(tools: List[Tool], allow_writes: bool) -> List[Tool]:
    """The tools a sub-agent may use: read-only ones, or every one with `allow_writes`."""
    chosen = []
    for tool in tools:
        if tool.__name__ in EXCLUDED_TOOLS:
            continue
        if allow_writes or getattr(tool, "max_level", PermissionLevel.WRITE) <= PermissionLevel.READ:
            chosen.append(tool)
    return chosen


class SubAgents:
    """Runs the sub-agents of a session.

    `tools()` gives the tools the main agent has now (those turned off with ``/tools`` stay off).
    """

    def __init__(
        self,
        session: AgentSession,
        tools: Callable[[], List[Tool]],
        location: Callable[[], str] = lambda: "",
    ) -> None:
        self.session = session
        self.tools = tools
        self.location = location

    def run(self, description: str, prompt: str, allow_writes: bool = False) -> ToolResult:
        session = self.session
        view, parent = session.view, session.current_tool
        lines: List[str] = []

        def report(line: str) -> None:
            lines.append(line)
            if view is not None and parent is not None:
                view.add_detail(parent, line)

        def activity(what: str) -> None:
            if view is not None:
                view.activity = f"{description} · {what}"

        chosen = subagent_tools(self.tools(), allow_writes)
        by_name = {tool.__name__: tool for tool in chosen}

        def execute(function_name: str, arguments: Dict[str, Any]) -> str:
            if session.cancelled:
                return ToolResult.fail(RuntimeError("Interrupted by the user.")).to_llm()
            activity(f"running {function_name}")
            started = time.monotonic()
            function = by_name.get(function_name)
            try:
                if function is None:
                    raise LookupError(f"{function_name} is not an available tool.")
                outcome = function(**arguments)
            except Exception as error:  # a tool must never break the agent loop
                outcome = ToolResult.fail(error)
            duration = time.monotonic() - started
            result = outcome if isinstance(outcome, ToolResult) else ToolResult.ok(outcome)

            summary = tool_display(result)[0]
            line = f"{'✔' if result.success else '✘'} {function_name}({format_arguments(arguments)}) · {duration:.1f}s"
            if summary:
                line += f" · {summary}"
            if not result.success:
                line += f" — {result.error}"
            report(line)
            activity("thinking")
            session.journal.log_tool_call(
                f"task:{function_name}",
                arguments,
                result.to_dict(),
                duration,
                error=None if result.success else str(result.error),
            )
            return result.to_llm()

        instructions = (
            f"{INSTRUCTIONS}Current date and time: {describe_now(session.clock())}\n"
            f"Working directory: {self.location()}"
        )
        body = session.remote.client.body(  # type: ignore[union-attr]
            prompt,
            session.remote.client.conversation_id(f"sub-{uuid.uuid4().hex}"),  # type: ignore[union-attr]
            ephemeral=True,
            instructions=instructions,
            tools=session.remote.schemas(chosen),  # type: ignore[union-attr]
        )
        answer: List[str] = []

        def on_text(chunk: str) -> None:
            answer.append(chunk)
            if view is not None:
                view.count_chunk()  # generated, but not shown: only the final report goes back

        def on_usage(prompt_tokens: int, completion_tokens: int) -> None:
            if view is not None:
                view.request_finished(completion_tokens)

        activity("thinking")
        done: Optional[Dict[str, Any]] = None
        started = time.perf_counter()
        try:
            done = run_remote_turn(
                session.remote.client,  # type: ignore[union-attr]
                body,
                execute,
                on_text=on_text,
                on_usage=on_usage,
                cancel=session._cancel,
            )
        except ClaraError as error:
            session.journal.log_error(f"sub-agent: {error}")
            return ToolResult.fail(error, {"partial_report": "".join(answer)})
        if done is not None:
            self._account(done, time.perf_counter() - started)

        if session.cancelled:
            return ToolResult.fail(RuntimeError("Interrupted by the user."))
        text = "".join(answer).strip()
        if not text:
            return ToolResult.fail(RuntimeError("The sub-agent gave no answer."))
        detail = "\n".join([*lines, "", text]) if lines else text
        return ToolResult.ok({"report": text}, summary=f"{len(lines)} tool call(s)", detail=detail)

    def _account(self, done: Dict[str, Any], duration: float) -> None:
        """The sub-agent's tokens go to the usage ledger like any other run."""
        usage = done.get("usage") or {}
        prompt_tokens, completion_tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        stats = TurnStats(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, duration)
        self.session.usage.record(self.session.model, stats)
