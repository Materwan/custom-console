"""Shared plumbing for the agent tools.

NOTE: the tool modules deliberately do not use ``from __future__ import
annotations``: agno builds the JSON schema of each tool from its real type
hints, which must therefore be evaluated when the function is defined.
"""

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Union

from ...fs import Backend, FileManager
from ...settings import Settings
from ..cache import JsonCache
from ..checkpoints import Checkpoints
from ..permissions import PermissionGate, PermissionLevel, UserPermissionDenied, describe_call
from ..results import ToolResult
from ..zone import FreeZone
from .state import ReadTracker, TodoList

# A tool's level is fixed, or computed from its arguments (e.g. free inside the zone).
LevelRule = Union[PermissionLevel, Callable[..., PermissionLevel]]


@dataclass
class ToolContext:
    """Everything the tools need, injected once when they are built."""

    settings: Settings
    files: FileManager
    gate: PermissionGate
    cache: JsonCache
    zone: FreeZone = field(default_factory=FreeZone)
    checkpoints: Optional[Checkpoints] = None
    reads: ReadTracker = field(default_factory=ReadTracker)
    todos: TodoList = field(default_factory=TodoList)
    is_cancelled: Callable[[], bool] = lambda: False
    # ask_user(question, choices, multiple, allow_other) -> Answer, or None when skipped
    ask_user: Optional[Callable[..., Any]] = None
    # run_subagent(description, prompt, allow_writes) -> ToolResult
    run_subagent: Optional[Callable[..., Any]] = None
    _closers: list = field(default_factory=list)

    def on_close(self, callback: Callable[[], None]) -> None:
        """Register a cleanup to run when the agent console shuts down."""
        self._closers.append(callback)

    def close(self) -> None:
        for callback in reversed(self._closers):
            try:
                callback()
            except Exception:
                pass
        self._closers.clear()

    # -- free zone ----------------------------------------------------------- #

    def in_zone(self, raw: str, *, strict: bool = False) -> bool:
        """Is the path inside the free zone? Anything that cannot be resolved to a
        local path (reMarkable, virtual root, errors) is outside."""
        try:
            target = self.files.resolve(raw)
        except Exception:
            return False
        return target.backend is Backend.LOCAL and self.zone.contains(target.path, strict=strict)

    def snapshot(self, path: str) -> None:
        """Save `path` for ``/undo`` before it is changed."""
        if self.checkpoints is not None:
            self.checkpoints.backup(path)


def zone_level(
    ctx: ToolContext, level: PermissionLevel, *params: str, strict: Tuple[str, ...] = ()
) -> Callable[..., PermissionLevel]:
    """Level rule: NONE when every path argument named in `params` is inside the
    free zone, `level` otherwise. `strict` names the arguments for which the zone
    folder itself does not count. Arguments left unset (None) are ignored."""

    def rule(**arguments: Any) -> PermissionLevel:
        for name in params:
            value = arguments.get(name)
            if value is None:
                continue
            if not ctx.in_zone(value, strict=name in strict):
                return level
        return PermissionLevel.NONE

    rule.max_level = level  # type: ignore[attr-defined]
    return rule


def guarded(ctx: ToolContext, level: LevelRule, *, describe: Optional[Callable[..., str]] = None):
    """Decorator: ask the permission gate before running a tool, and turn any
    exception into a failed :class:`ToolResult`.

    `level` is a :class:`PermissionLevel` or a function of the tool's arguments
    (defaults included). `describe(**arguments)` may customise the sentence shown
    to the user. Neither may break the tool: on error the level becomes WRITE and
    the sentence falls back to the generic one.
    """

    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            given: Dict[str, Any] = dict(bound.arguments)
            bound.apply_defaults()
            full: Dict[str, Any] = dict(bound.arguments)

            try:
                needed = level(**full) if callable(level) else level
            except Exception:
                needed = PermissionLevel.WRITE
            try:
                info = describe(**full) if describe else describe_call(func.__name__, given)
            except Exception:
                info = describe_call(func.__name__, given)

            if not ctx.gate.request(info, needed):
                return ToolResult.fail(UserPermissionDenied(f"The user refused: {func.__name__}"))
            try:
                return func(*args, **kwargs)
            except Exception as error:  # tools report failures, they never raise
                return ToolResult.fail(error)

        # The most a call can need: READ or less means the tool changes nothing.
        wrapper.max_level = getattr(level, "max_level", PermissionLevel.WRITE) if callable(level) else level  # type: ignore[attr-defined]
        return wrapper

    return decorator


def clip(text: str, limit: int) -> Tuple[str, bool]:
    """`text` cut to `limit` characters, plus whether it was truncated."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def truncation_notice(limit: int) -> str:
    return f"\n[... truncated to {limit} characters ...]"
