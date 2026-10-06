"""Shared plumbing for the agent tools.

NOTE: the tool modules deliberately do not use ``from __future__ import
annotations``: `schema.py` builds the JSON schema of each tool from its real
type hints, which are simplest to read when they are evaluated at definition.
"""

import functools
import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Union

from ...fs import Backend, FileManager
from ...settings import Settings
from ..cache import JsonCache
from ..checkpoints import Checkpoints
from ..permissions import (
    PermissionGate,
    PermissionLevel,
    UserPermissionDenied,
    describe_call,
    last_given,
    refusal_message,
)
from ..results import ToolResult
from ..schema import coerce_arguments
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
    # progress(line): a line of what the running tool is doing, shown under its line (a command's output)
    progress: Callable[[str], None] = lambda line: None
    # the model's context window in tokens (what a tool may return is a share of it)
    window: Callable[[], int] = lambda: 32_768
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

    def in_zone(self, raw: str, *, strict: bool = False, write: bool = False) -> bool:
        """Is the path inside the free zone (and, with `write`, not one of its protected paths)?
        Anything that cannot be resolved to a local path (reMarkable, virtual root, errors) is outside."""
        try:
            target = self.files.resolve(raw)
        except Exception:
            return False
        return target.backend is Backend.LOCAL and self.zone.contains(target.path, strict=strict, write=write)

    def max_chars(self, share: float = 0.25) -> int:
        """Characters a tool may give back: `share` of the context window (about 3.5 characters a token)."""
        try:
            window = int(self.window())
        except Exception:
            window = 32_768
        return max(4_000, int(window * 3.5 * share))

    def snapshot(self, path: str) -> None:
        """Save `path` for ``/undo`` before it is changed."""
        if self.checkpoints is not None:
            self.checkpoints.backup(path)


def zone_level(
    ctx: ToolContext, level: PermissionLevel, *params: str, strict: Tuple[str, ...] = ()
) -> Callable[..., PermissionLevel]:
    """Level rule: NONE when every path argument named in `params` is inside the
    free zone, `level` otherwise. `strict` names the arguments for which the zone
    folder itself does not count. Arguments left unset (None) are ignored. For a
    WRITE level the protected paths of the zone are outside it."""

    def rule(**arguments: Any) -> PermissionLevel:
        for name in params:
            value = arguments.get(name)
            if value is None:
                continue
            if not ctx.in_zone(value, strict=name in strict, write=level >= PermissionLevel.WRITE):
                return level
        return PermissionLevel.NONE

    rule.max_level = level  # type: ignore[attr-defined]
    rule.paths = params  # type: ignore[attr-defined]  (what an "always" answer is about: see default_rule)
    return rule


def default_rule(ctx: ToolContext, name: str, level: Any, arguments: Dict[str, Any]) -> Optional[str]:
    """What an "always" answer covers: the tool, in the folder of its (last) path argument when it has one."""
    paths = getattr(level, "paths", ())
    value = last_given(arguments, paths)
    if value is None:
        return name
    target = ctx.files.resolve(str(value))
    if target.backend is not Backend.LOCAL:
        return name
    return f"{name}:{os.path.dirname(os.path.abspath(target.path)) or target.path}"


def guarded(
    ctx: ToolContext,
    level: LevelRule,
    *,
    describe: Optional[Callable[..., str]] = None,
    rule: Optional[Callable[..., Optional[str]]] = None,
):
    """Decorator: ask the permission gate before running a tool, and turn any
    exception into a failed :class:`ToolResult`.

    `level` is a :class:`PermissionLevel` or a function of the tool's arguments
    (defaults included). `describe(**arguments)` may customise the sentence shown
    to the user, and `rule(**arguments)` what an "always" answer covers (None: the
    call is always asked; default: see :func:`default_rule`). None of them may break
    the tool: on error the level becomes WRITE, the sentence falls back to the
    generic one and the call gets no rule. The arguments are first converted to the
    parameters' types when a model sent "2" for 2 or "true" for true.
    """

    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            given: Dict[str, Any] = coerce_arguments(func, dict(signature.bind(*args, **kwargs).arguments))
            bound = signature.bind(**given)
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

            try:
                key = rule(**full) if rule else default_rule(ctx, func.__name__, level, full)
            except Exception:
                key = None

            decision = ctx.gate.request(info, needed, key)
            if not decision:
                return ToolResult.fail(UserPermissionDenied(refusal_message(func.__name__, decision)))
            try:
                return func(**given)
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
