"""What every local tool shares: its description (`ToolSpec`), what it runs with (`ToolContext`), its errors, the
argument check and the shaping of a result."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ...platform import CancelToken, Platform
from ..permissions.engine import CallInfo
from ..permissions.floor import FloorConfig
from ..vfs.guard import OverwriteGuard
from ..vfs.ops import Vfs
from ..vfs.resolver import Resolved


class ToolError(Exception):
    """A call that cannot be done. The message goes back to the model as the result, so it says what to do instead."""


# ---------------------------------------------------------------------- hooks the interface provides
@dataclass(frozen=True, slots=True)
class Question:
    text: str
    options: tuple[str, ...] = ()


AskHook = Callable[[Question], Awaitable[str]]
SubagentHook = Callable[[str, CancelToken], Awaitable[str]]


@dataclass(slots=True)
class Todo:
    content: str
    status: str = "pending"  # pending, in_progress or completed


@dataclass
class TodoStore:
    items: list[Todo] = field(default_factory=list)

    def replace(self, items: list[Todo]) -> None:
        self.items = items

    def render(self) -> str:
        if not self.items:
            return "The checklist is empty."
        marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        return "\n".join(f"{marks.get(t.status, '[ ]')} {t.content}" for t in self.items)


@dataclass
class ToolContext:
    """Everything a handler may use. Built once per session; the interface fills the hooks."""

    vfs: Vfs
    platform: Platform
    folder: Resolved  # the agent's working folder
    trusted: bool
    cancel: CancelToken
    guard: OverwriteGuard
    floor: FloorConfig
    env: Mapping[str, str]  # the scrubbed environment given to every child process
    tool_timeout: float = 120.0
    result_limit: int = 30_000
    undo: Any = None  # core.undo.UndoLog
    jobs: Any = None  # core.tools.run.JobTable
    todos: TodoStore = field(default_factory=TodoStore)
    ask: AskHook | None = None
    subagent: SubagentHook | None = None
    git: str | None = None  # path to the git executable, None when git is not installed
    extras: dict[str, Any] = field(default_factory=dict)  # per-call scratch (for example, an app the finder found)
    round_deadline: float | None = None  # time.monotonic() by which the round's results must be posted

    def seconds_left(self) -> float | None:
        return None if self.round_deadline is None else max(self.round_deadline - time.monotonic(), 0.0)


Handler = Callable[[ToolContext, dict[str, Any]], Any]  # returns str, or an awaitable of str
Inspector = Callable[[ToolContext, dict[str, Any]], CallInfo]
Previewer = Callable[[ToolContext, dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: str  # one of the engine's kinds
    inspect: Inspector
    run: Handler
    preview: Previewer | None = None  # the diff or details the ask prompt shows
    read_only_for_subagents: bool = False
    needs_trusted_folder: bool = False  # write, run and git tools: not offered in a folder that is not trusted
    available: Callable[[ToolContext], str] | None = None  # why the tool cannot be used right now, "" when it can
    default_enabled: bool = True

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,), "object": (dict,),
}


def validate_args(schema: Mapping[str, Any], args: Any) -> list[str]:
    """Problems with the arguments a model sent (empty: fine). Only the shape is checked."""
    if not isinstance(args, dict):
        return ["the arguments must be an object"]
    problems: list[str] = []
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            problems.append(f"{name} is required")
    for name, value in args.items():
        spec = properties.get(name)
        if spec is None:
            problems.append(f"{name} is not a parameter of this tool")
            continue
        expected = _TYPES.get(spec.get("type", ""))
        if expected is None or value is None:
            continue
        if (isinstance(value, bool) and bool not in expected) or not isinstance(value, expected):
            problems.append(f"{name} must be {spec['type']}")
    return problems


async def call_handler(spec: ToolSpec, context: ToolContext, args: dict[str, Any]) -> str:
    """Run a handler and make sure the result is text. A plain function runs in a worker thread, so a slow search or a long
    command never freezes the interface; an `async def` handler runs on the event loop."""
    if inspect.iscoroutinefunction(spec.run):
        result = await spec.run(context, args)
    else:
        result = await asyncio.to_thread(spec.run, context, args)
        if inspect.isawaitable(result):
            result = await result
    return result if isinstance(result, str) else str(result)


def cut_middle(text: str, limit: int) -> str:
    """At most about `limit` characters, keeping the start and the end, with a note of how much was left out."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(limit - head, 0)
    left_out = len(text) - head - tail
    note = f"\n[... {left_out:,} characters left out ...]\n"
    return text[:head] + note + (text[-tail:] if tail else "")
