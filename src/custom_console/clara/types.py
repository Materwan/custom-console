"""The shapes the console sends to Clara and reads back.

Bodies are built from typed models: a field is left out only when it is `None`, never because it is falsy (0 and
False are values). Answers are read leniently: an unknown field is ignored, a missing one takes its default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CONVERSATION_ID = re.compile(r"^c[0-9a-f]{16}$")
TOKEN_SHAPE = re.compile(r"^clara_[A-Za-z0-9]+_[A-Za-z0-9_\-]+$")
ANSWERS = ("allow_once", "allow_always", "refuse_once", "refuse_always")

MAX_MESSAGE = 2_000_000
MAX_TOOLS = 64
MAX_INSTRUCTIONS = 20_000
MAX_ATTACHMENT_BYTES = 30_000_000


def _str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _int(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    data: str  # the file's bytes, base64


@dataclass(frozen=True, slots=True)
class ChatBody:
    """The body of `POST /v1/chat/stream`."""

    message: str = ""
    conversation: str | None = None
    attachments: tuple[Attachment, ...] = ()
    project: int | None = None
    model: str | None = None
    fork_of: int | None = None
    timezone: str | None = None
    tools: tuple[dict[str, Any], ...] = ()
    instructions: str | None = None
    incognito: bool | None = None
    local_only: bool | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": self.message,
            "conversation": self.conversation,
            "attachments": [{"name": a.name, "data": a.data} for a in self.attachments] if self.attachments else None,
            "project": self.project,
            "model": self.model,
            "fork_of": self.fork_of,
            "timezone": self.timezone,
            "tools": list(self.tools) if self.tools else None,
            "instructions": self.instructions,
            "incognito": self.incognito,
            "local_only": self.local_only,
        }
        return {key: value for key, value in body.items() if value is not None}

    def problems(self) -> list[str]:
        """What Clara would refuse, found before sending."""
        found: list[str] = []
        if not self.message.strip() and not self.attachments:
            found.append("The message is empty.")
        if len(self.message) > MAX_MESSAGE:
            found.append(f"The message is longer than {MAX_MESSAGE:,} characters.")
        if self.conversation is not None and not CONVERSATION_ID.match(self.conversation):
            found.append(f"{self.conversation!r} is not a conversation id.")
        if len(self.tools) > MAX_TOOLS:
            found.append(f"{len(self.tools)} tools; Clara takes {MAX_TOOLS} at most.")
        if self.instructions is not None and len(self.instructions) > MAX_INSTRUCTIONS:
            found.append(f"The instructions are {len(self.instructions):,} characters; Clara takes {MAX_INSTRUCTIONS:,}.")
        return found


@dataclass(frozen=True, slots=True)
class Me:
    token: str = ""
    client: str = ""
    approvals: str = ""
    web: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Me:
        return cls(_str(data, "token"), _str(data, "client"), _str(data, "approvals"), _bool(data, "web"))


@dataclass(frozen=True, slots=True)
class ModelInfo:
    ref: str
    name: str = ""
    provider: str = ""
    local: bool = False
    window: int = 0
    vision: bool | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ModelInfo:
        capabilities = data.get("capabilities")
        vision = capabilities.get("vision") if isinstance(capabilities, dict) else None
        return cls(_str(data, "ref"), _str(data, "name"), _str(data, "provider"), _bool(data, "local"),
                   _int(data, "window"), vision if isinstance(vision, bool) else None)


@dataclass(frozen=True, slots=True)
class Models:
    models: tuple[ModelInfo, ...] = ()
    default: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Models:
        shown = data.get("models")
        items = tuple(ModelInfo.from_json(m) for m in shown if isinstance(m, dict)) if isinstance(shown, list) else ()
        return cls(items, _str(data, "default"))


@dataclass(frozen=True, slots=True)
class Pack:
    id: str
    label: str = ""
    description: str = ""
    default: bool = False
    available: bool = True
    reason: str = ""
    where: str = "both"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Pack:
        return cls(_str(data, "id"), _str(data, "label"), _str(data, "description"), _bool(data, "default"),
                   _bool(data, "available", True), _str(data, "reason"), _str(data, "where", "both"))


@dataclass(frozen=True, slots=True)
class Project:
    id: int
    name: str = ""
    description: str = ""
    files: int = 0

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Project:
        return cls(_int(data, "id"), _str(data, "name"), _str(data, "description"), _int(data, "files"))


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    title: str = ""
    project: int | None = None
    model: str = ""
    incognito: bool = False
    local_only: bool = False
    created_at: str = ""
    updated_at: str = ""
    packs: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Conversation:
        packs = data.get("packs")
        project = data.get("project")
        return cls(_str(data, "id"), _str(data, "title"), project if isinstance(project, int) else None,
                   _str(data, "model"), _bool(data, "incognito"), _bool(data, "local_only"), _str(data, "created_at"),
                   _str(data, "updated_at"), tuple(p for p in packs if isinstance(p, str)) if isinstance(packs, list) else ())


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    role: str
    content: str
    versions: tuple[int, ...] = ()
    version: int = 0
    created_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Message:
        versions = data.get("versions")
        return cls(_int(data, "id"), _str(data, "role"), _str(data, "content"),
                   tuple(v for v in versions if isinstance(v, int)) if isinstance(versions, list) else (),
                   _int(data, "version"), _str(data, "created_at"), data)


@dataclass(frozen=True, slots=True)
class Approval:
    id: int
    tool: str = ""
    target: str = ""
    level: str = ""
    summary: str = ""
    reason: str = ""
    status: str = ""
    conversation: str = ""
    expires_at: str = ""
    may_answer: bool = True

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Approval:
        return cls(_int(data, "id"), _str(data, "tool"), _str(data, "target"), _str(data, "level"),
                   _str(data, "summary"), _str(data, "reason"), _str(data, "status"), _str(data, "conversation"),
                   _str(data, "expires_at"), _bool(data, "may_answer", True))


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    effect: str = ""
    tool: str = ""
    target: str = ""
    situation: str = ""
    level: str = ""
    note: str = ""
    enabled: bool = True
    origin: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Rule:
        return cls(_int(data, "id"), _str(data, "effect"), _str(data, "tool"), _str(data, "target"),
                   _str(data, "situation"), _str(data, "level"), _str(data, "note"), _bool(data, "enabled", True),
                   _str(data, "origin"))


@dataclass(frozen=True, slots=True)
class Rules:
    rules: tuple[Rule, ...] = ()
    defaults: tuple[dict[str, Any], ...] = ()
    tainted_rule: bool | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Rules:
        rules = data.get("rules")
        defaults = data.get("defaults")
        tainted = data.get("tainted_rule")
        return cls(tuple(Rule.from_json(r) for r in rules if isinstance(r, dict)) if isinstance(rules, list) else (),
                   tuple(d for d in defaults if isinstance(d, dict)) if isinstance(defaults, list) else (),
                   tainted if isinstance(tainted, bool) else None)
