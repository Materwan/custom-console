"""Saved sessions of the agent, one set per working directory (``/restore``).

A session is one conversation: the questions and answers as they were shown, the
model and tools it used, its compaction summary, its checklist. It is written
after every turn to ``<agent dir>/sessions/<directory key>/<session id>.json``
(atomically), and only the most recent ones of a directory are kept. The model's
own memory of the conversation stays on the Clara server, under the conversation id
recorded here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")
FIRST_PROMPT_LIMIT = 70


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


@dataclass
class SessionRecord:
    id: str
    directory: str
    started: str = field(default_factory=now_iso)
    updated: str = ""
    model: str = ""
    provider: str = ""  # the Clara server's provider when the session was saved (informational)
    permission_level: int = 1
    disabled_tools: List[str] = field(default_factory=list)
    todos: List[Dict[str, Any]] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)  # the server conversation id, its summary
    turns: List[Dict[str, Any]] = field(default_factory=list)  # TurnView.to_dict() of each turn

    @property
    def first_prompt(self) -> str:
        if not self.turns:
            return ""
        lines = str(self.turns[0].get("prompt", "")).strip().splitlines()
        text = lines[0] if lines else ""
        return text if len(text) <= FIRST_PROMPT_LIMIT else text[: FIRST_PROMPT_LIMIT - 1] + "…"

    @property
    def when(self) -> datetime:
        for stamp in (self.updated, self.started):
            try:
                return datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
        return datetime.fromtimestamp(0).astimezone()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "directory": self.directory,
            "started": self.started,
            "updated": self.updated,
            "model": self.model,
            "provider": self.provider,
            "permission_level": self.permission_level,
            "disabled_tools": list(self.disabled_tools),
            "todos": list(self.todos),
            "context": dict(self.context),
            "turns": list(self.turns),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionRecord":
        def listed(name: str) -> list:
            value = data.get(name)
            return value if isinstance(value, list) else []

        context = data.get("context")
        return cls(
            id=str(data["id"]),
            directory=str(data.get("directory", "")),
            started=str(data.get("started", "")),
            updated=str(data.get("updated", "")),
            model=str(data.get("model", "")),
            provider=str(data.get("provider") or ""),
            permission_level=int(data.get("permission_level", 1)),
            disabled_tools=[str(name) for name in listed("disabled_tools")],
            todos=[item for item in listed("todos") if isinstance(item, dict)],
            context=context if isinstance(context, dict) else {},
            turns=[turn for turn in listed("turns") if isinstance(turn, dict)],
        )

    def conversation_id(self) -> str:
        """The conversation on the Clara server (sessions saved by older versions: "base")."""
        return str(self.context.get("conversation") or self.context.get("base") or "")


def directory_key(directory: "str | os.PathLike[str]") -> str:
    real = os.path.normcase(os.path.realpath(os.fspath(directory)))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


class SessionStore:
    """The saved sessions of one working directory."""

    def __init__(self, root: Path, directory: Optional[str], keep: int = 5):
        self.root = Path(root)
        self.directory = directory
        self.keep = max(1, keep)

    @property
    def available(self) -> bool:
        return bool(self.directory)

    @property
    def folder(self) -> Path:
        assert self.directory is not None
        return self.root / directory_key(self.directory)

    def new_record(self) -> SessionRecord:
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        return SessionRecord(identifier, self.directory or "")

    def _path(self, identifier: str) -> Path:
        if not SAFE_ID.match(identifier):
            raise ValueError(f"invalid session id: {identifier!r}")
        return self.folder / f"{identifier}.json"

    def save(self, record: SessionRecord) -> List[SessionRecord]:
        """Write `record`, then drop the oldest sessions beyond `keep`. Returns the
        sessions that were dropped. Never raises: a failing disk only loses history."""
        if not self.available:
            return []
        record.updated = now_iso()
        try:
            path = self._path(record.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except (OSError, ValueError):
            return []
        dropped = [old for old in self.list()[self.keep :] if old.id != record.id]
        for old in dropped:
            self.delete(old.id)
        return dropped

    def load(self, identifier: str) -> Optional[SessionRecord]:
        if not self.available:
            return None
        try:
            data = json.loads(self._path(identifier).read_text(encoding="utf-8"))
            return SessionRecord.from_dict(data)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def list(self) -> List[SessionRecord]:
        """The saved sessions, the most recent first (damaged files are skipped)."""
        if not self.available or not self.folder.is_dir():
            return []
        records = [record for path in self.folder.glob("*.json") if (record := self.load(path.stem)) is not None]
        return sorted(records, key=lambda record: record.when, reverse=True)

    def delete(self, identifier: str) -> None:
        try:
            self._path(identifier).unlink()
        except (OSError, ValueError):
            pass


def ago(moment: datetime, now: Optional[datetime] = None) -> str:
    """``5 min ago``, ``3 h ago``, ``2 days ago``."""
    seconds = max(0, int(((now or datetime.now().astimezone()) - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"
