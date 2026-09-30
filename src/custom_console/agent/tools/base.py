"""Shared plumbing for the agent tools.

NOTE: the tool modules deliberately do not use ``from __future__ import
annotations``: agno builds the JSON schema of each tool from its real type
hints, which must therefore be evaluated when the function is defined.
"""

import functools
import inspect
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from ...fs import FileManager
from ...settings import Settings
from ..cache import JsonCache
from ..permissions import PermissionGate, PermissionLevel, UserPermissionDenied, describe_call
from ..results import ToolResult
from ..workspace import Workspace


@dataclass
class ToolContext:
    """Everything the tools need, injected once when they are built."""

    settings: Settings
    files: FileManager
    gate: PermissionGate
    workspace: Workspace
    cache: JsonCache
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


def guarded(ctx: ToolContext, level: PermissionLevel, *, describe: Optional[Callable[..., str]] = None):
    """Decorator: ask the permission gate before running a tool, and turn any
    exception into a failed :class:`ToolResult`.

    `describe(**arguments)` may customise the sentence shown to the user.
    """

    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            arguments = dict(bound.arguments)
            info = describe(**arguments) if describe else describe_call(func.__name__, arguments)

            if not ctx.gate.request(info, level):
                return ToolResult.fail(UserPermissionDenied(f"The user refused: {func.__name__}"))
            try:
                return func(*args, **kwargs)
            except Exception as error:  # tools report failures, they never raise
                return ToolResult.fail(error)

        return wrapper

    return decorator


def clip(text: str, limit: int) -> Tuple[str, bool]:
    """`text` cut to `limit` characters, plus whether it was truncated."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def truncation_notice(limit: int) -> str:
    return f"\n[... truncated to {limit} characters ...]"

