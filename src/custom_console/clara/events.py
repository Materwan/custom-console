"""The events of a turn, as typed values.

Parsing ignores unknown fields and turns an unknown event type (or a malformed one) into `UnknownEvent`, so a newer
Clara does not break an older console.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import ClaraError
from .types import Approval, _bool, _float, _int, _str


@dataclass(frozen=True, slots=True)
class TurnStarted:
    id: str
    conversation: str | None
    model: str
    incognito: bool
    local_only: bool


@dataclass(frozen=True, slots=True)
class Thinking:
    text: str


@dataclass(frozen=True, slots=True)
class Token:
    text: str


@dataclass(frozen=True, slots=True)
class Retrying:
    message: str
    model: str


@dataclass(frozen=True, slots=True)
class ToolStart:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolDone:
    name: str
    arguments: dict[str, Any]
    result: str
    truncated: bool
    approval: int | None


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    approval: Approval


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolRequests:
    turn: str
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int


@dataclass(frozen=True, slots=True)
class Compacted:
    before: float
    after: float


@dataclass(frozen=True, slots=True)
class Discarded:
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Warning:
    message: str


@dataclass(frozen=True, slots=True)
class Quiz:
    """A quiz (QCM) Clara showed. The console cannot draw one; it only says so."""

    title: str


@dataclass(frozen=True, slots=True)
class FileNote:
    path: str
    action: str


@dataclass(frozen=True, slots=True)
class Done:
    reply: str
    conversation: str | None
    tools: tuple[str, ...]
    message_id: int | None
    versions: tuple[int, ...]
    model: str
    tainted: bool
    context_tokens: int
    context_window: int
    context_percent: float
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    status: int
    code: str
    message: str

    def as_error(self) -> ClaraError:
        return ClaraError(self.code, self.message, self.status)


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict, compare=False)


Event = (
    TurnStarted | Thinking | Token | Retrying | ToolStart | ToolDone | ApprovalRequested | ToolRequests | Usage
    | Compacted | Discarded | Warning | Quiz | FileNote | Done | ErrorEvent | UnknownEvent
)


def _dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _strings(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _turn(d: dict[str, Any]) -> Event:
    return TurnStarted(_str(d, "id"), _optional_str(d, "conversation"), _str(d, "model"), _bool(d, "incognito"),
                       _bool(d, "local_only"))


def _tool_done(d: dict[str, Any]) -> Event:
    approval = d.get("approval")
    return ToolDone(_str(d, "name"), _dict(d, "arguments"), _str(d, "result"), _bool(d, "truncated"),
                    approval if isinstance(approval, int) and not isinstance(approval, bool) else None)


def _tool_requests(d: dict[str, Any]) -> Event:
    calls = d.get("calls")
    parsed = tuple(ToolCall(_str(c, "id"), _str(c, "name"), _dict(c, "arguments"))
                   for c in calls if isinstance(c, dict)) if isinstance(calls, list) else ()
    if not _str(d, "turn") or not all(call.id and call.name for call in parsed):
        raise ValueError("tool_requests without a turn or a call id")
    return ToolRequests(_str(d, "turn"), parsed)


def _usage(d: dict[str, Any]) -> Event:
    return Usage(_int(d, "prompt_tokens"), _int(d, "completion_tokens"), _int(d, "cached_tokens"))


def _quiz(d: dict[str, Any]) -> Event:
    return Quiz(_str(_dict(d, "form"), "title"))


def _done(d: dict[str, Any]) -> Event:
    usage, context = _dict(d, "usage"), _dict(d, "context")
    message_id = d.get("message_id")
    versions = d.get("versions")
    return Done(
        _str(d, "reply"), _optional_str(d, "conversation"), _strings(d, "tools"),
        message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
        tuple(v for v in versions if isinstance(v, int)) if isinstance(versions, list) else (),
        _str(d, "model"), _bool(d, "tainted"), _int(context, "tokens"), _int(context, "window"),
        _float(context, "percent"), _int(usage, "prompt_tokens"), _int(usage, "completion_tokens"),
    )


_FACTORIES: dict[str, Callable[[dict[str, Any]], Event]] = {
    "turn": _turn,
    "thinking": lambda d: Thinking(_str(d, "text")),
    "token": lambda d: Token(_str(d, "text")),
    "retrying": lambda d: Retrying(_str(d, "message"), _str(d, "model")),
    "tool_start": lambda d: ToolStart(_str(d, "name"), _dict(d, "arguments")),
    "tool": _tool_done,
    "approval": lambda d: ApprovalRequested(Approval.from_json(_dict(d, "approval"))),
    "tool_requests": _tool_requests,
    "usage": _usage,
    "compacted": lambda d: Compacted(_float(d, "before"), _float(d, "after")),
    "discarded": lambda d: Discarded(_strings(d, "tools")),
    "warning": lambda d: Warning(_str(d, "message")),
    "qcm": _quiz,
    "file": lambda d: FileNote(_str(d, "path"), _str(d, "action")),
    "done": _done,
    "error": lambda d: ErrorEvent(_int(d, "status"), _str(d, "code", "error"), _str(d, "message")),
}


def parse_event(data: dict[str, Any]) -> Event:
    kind = data.get("type")
    factory = _FACTORIES.get(kind) if isinstance(kind, str) else None
    if factory is None:
        return UnknownEvent(str(kind), data)
    try:
        return factory(data)
    except (TypeError, ValueError, KeyError, AttributeError):
        return UnknownEvent(str(kind), data)
