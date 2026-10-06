"""Shared plumbing for the agent tools.

NOTE: the tool modules deliberately do not use ``from __future__ import
annotations``: agno builds the JSON schema of each tool from its real type
hints, which must therefore be evaluated when the function is defined.
"""

import functools
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...fs import FileManager
from ...settings import Settings
from ..cache import JsonCache
from ..permissions import PermissionGate, PermissionLevel, UserPermissionDenied, describe_call
from ..results import ToolResult
from ..workspace import Workspace


class ToolMemo:
    """Short-lived memory of identical read calls (phase 4 of the token roadmap).

    An agent often repeats `list`/`read` with the same arguments inside a few
    seconds; answering from here saves the call, its permission question and,
    above all, a duplicated result in the history. Anything that can change what
    a read returns (a write, a `cd`) clears it.
    """

    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._items: Dict[str, Tuple[float, Any]] = {}

    @staticmethod
    def key(name: str, arguments: Dict[str, Any]) -> str:
        return name + json.dumps(arguments, sort_keys=True, default=str)

    def get(self, key: str) -> Optional[Any]:
        item = self._items.get(key)
        if item is None:
            return None
        if self._clock() - item[0] > self.ttl:
            del self._items[key]
            return None
        return item[1]

    def put(self, key: str, value: Any) -> None:
        self._items[key] = (self._clock(), value)

    def clear(self) -> None:
        self._items.clear()


@dataclass
class ToolContext:
    """Everything the tools need, injected once when they are built."""

    settings: Settings
    files: FileManager
    gate: PermissionGate
    workspace: Workspace
    cache: JsonCache
    memo: ToolMemo = field(default_factory=ToolMemo)
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


def guarded(
    ctx: ToolContext,
    level: PermissionLevel,
    *,
    describe: Optional[Callable[..., str]] = None,
    memo: bool = False,
):
    """Decorator: ask the permission gate before running a tool, and turn any
    exception into a failed :class:`ToolResult`.

    `describe(**arguments)` may customise the sentence shown to the user.
    `memo` answers a repeated identical call from `ctx.memo` (reads only); a
    WRITE tool clears the memo once it has run.
    """

    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            arguments = dict(bound.arguments)
            key = ToolMemo.key(func.__name__, arguments) if memo else ""
            if memo:
                remembered = ctx.memo.get(key)
                if remembered is not None:
                    return remembered
            info = describe(**arguments) if describe else describe_call(func.__name__, arguments)

            if not ctx.gate.request(info, level):
                return ToolResult.fail(UserPermissionDenied(f"The user refused: {func.__name__}"))
            try:
                result = func(*args, **kwargs)
            except Exception as error:  # tools report failures, they never raise
                result = ToolResult.fail(error)
            if level >= PermissionLevel.WRITE:
                ctx.memo.clear()
            elif memo and getattr(result, "success", False):
                ctx.memo.put(key, result)
            return result

        return wrapper

    return decorator


def clip(text: str, limit: int) -> Tuple[str, bool]:
    """`text` cut to `limit` characters, plus whether it was truncated."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def truncation_notice(limit: int, hint: str = "") -> str:
    """Tail appended to a cut text; `hint` says how to get the rest."""
    return f"\n[truncated at {limit} chars{'; ' + hint if hint else ''}]"


def cap_items(items: List[Any], limit: int, hint: str = "narrow the path or pattern") -> List[Any]:
    """The first `limit` items, plus one line saying how many were left out."""
    if len(items) <= limit:
        return items
    return [*items[:limit], f"[+{len(items) - limit} more; {hint}]"]

