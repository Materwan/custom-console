"""Permission levels and the gate that asks the user before risky tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Mapping

YES_ANSWERS = ("", "y", "yes", "o", "oui")  # an empty answer means yes


class PermissionLevel(IntEnum):
    """Risk of a tool. A tool is auto-accepted when its level is <= the
    auto-accept level configured for the session."""

    NONE = 0  # harmless (e.g. `pwd`): accepted without any trace
    READ = 1  # reads / consultation
    WRITE = 2  # writes, deletions, sending, clicking...


LEVEL_LABELS = {
    PermissionLevel.NONE: "always ask",
    PermissionLevel.READ: "reads are auto-accepted",
    PermissionLevel.WRITE: "everything is auto-accepted",
}


def permission_label(level: int) -> str:
    try:
        return LEVEL_LABELS[PermissionLevel(level)]
    except ValueError:
        return f"level {level}"


class UserPermissionDenied(Exception):
    """The user refused an action requested by the agent."""


def is_yes(answer: str) -> bool:
    return answer.strip().lower() in YES_ANSWERS


def _short(value: Any, limit: int) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(name: str, arguments: Mapping[str, Any], value_limit: int = 160) -> str:
    """Human-readable sentence for a tool call (long values are shortened)."""
    action = name.replace("_", " ")
    if not arguments:
        return f"Agent wants to {action}."
    shown = ", ".join(f"{key}={_short(value, value_limit)}" for key, value in arguments.items())
    return f"Agent wants to {action} with {shown}."


@dataclass
class PermissionGate:
    """Decides whether a tool may run.

    `ask` blocks until the user answers (it is called from the agent's worker
    thread); `record` is told about every decision so it can be shown.
    """

    auto_level: int
    ask: Callable[[str], bool]
    record: Callable[[str, str], None] = lambda info, status: None

    def request(self, info: str, level: int = PermissionLevel.READ) -> bool:
        if self.auto_level >= level:
            if level > PermissionLevel.NONE:
                self.record(info, "auto-accepted")
            return True
        granted = self.ask(info)
        self.record(info, "accepted" if granted else "refused")
        return granted
