"""Runs one agent turn: streams the answer into a TurnView and traces tool calls."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from .journal import JsonlLogger
from .results import ToolResult
from .turn import STYLE_ERROR, TurnStats, TurnView


class AgentSession:
    """Glue between an agno agent and the UI.

    `run_turn` is meant to run in a worker thread. The agent is bound after
    construction (`session.agent = ...`) because the tools it needs are built
    from objects that themselves refer to this session.
    """

    def __init__(self, journal: JsonlLogger, user_id: str, session_id: str):
        self.agent: Any = None
        self.journal = journal
        self.user_id = user_id
        self.session_id = session_id
        self._view: Optional[TurnView] = None
        self._cancel: Optional[threading.Event] = None

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
        view, cancel = self._view, self._cancel
        if cancel is not None and cancel.is_set():
            return ToolResult.fail(RuntimeError("Interrupted by the user.")).to_llm()

        note = view.tool_started(function_name, arguments) if view is not None else None
        started = time.monotonic()
        try:
            outcome = function_call(**arguments)
        except Exception as error:  # a tool must never break the agent loop
            outcome = ToolResult.fail(error)
        duration = time.monotonic() - started

        result = outcome if isinstance(outcome, ToolResult) else ToolResult.ok(outcome)
        if view is not None and note is not None:
            view.tool_finished(
                note,
                function_name,
                arguments,
                result.success,
                duration,
                None if result.success else str(result.error),
            )
        self.journal.log_tool_call(
            function_name,
            arguments,
            result.to_dict(),
            duration,
            error=None if result.success else str(result.error),
        )
        return result.to_llm()

    # -- turn ----------------------------------------------------------------- #

    def run_turn(self, view: TurnView, cancel: threading.Event) -> Optional[TurnStats]:
        """Stream the agent's answer to `view`. Never raises."""
        from agno.run.agent import RunEvent, RunOutput

        self._view, self._cancel = view, cancel
        self.journal.log_prompt(view.prompt)
        started = time.perf_counter()
        run_output = None
        try:
            for chunk in self.agent.run(
                view.prompt,
                stream=True,
                yield_run_output=True,
                user_id=self.user_id,
                session_id=self.session_id,
            ):
                if cancel.is_set():
                    break
                if isinstance(chunk, RunOutput):
                    run_output = chunk
                    continue
                event = getattr(chunk, "event", None)
                if event is not None and event != RunEvent.run_content:
                    continue
                content = getattr(chunk, "content", None)
                if isinstance(content, str) and content:
                    view.add_text(content)
        except Exception as error:
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
            self.journal.log_error(str(error))
        finally:
            self._view = self._cancel = None

        if cancel.is_set():
            view.add_note("Interrupted by the user.")
        self.journal.log_answer(view.answer_text())

        metrics = getattr(run_output, "metrics", None)
        if metrics is None:
            return None
        return TurnStats.from_metrics(metrics, time.perf_counter() - started)
