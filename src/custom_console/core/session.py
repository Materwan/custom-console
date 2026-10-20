"""A session: one Clara conversation seen from one folder.

Clara keeps the messages. The console saves a small file per session in `<data>/sessions/<folder hash>/`: conversation id,
title, times, model, tool toggles, checklist and totals. It is written after every turn, atomically. Only the newest 5 of a
folder are kept (the others stay on Clara and are never deleted). An incognito conversation is not saved at all.

A session file never holds permissions, rules or trust, so restoring one cannot raise anything (SEC-7).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.base import Todo, TodoStore
from .vfs.text import fold_path

KEEP_SESSIONS = 5


def timezone_name() -> str | None:
    """The computer's IANA time zone, or None (then Clara uses its own). The only place the time zone is decided."""
    try:
        import tzlocal
        name = tzlocal.get_localzone_name()
    except Exception:
        return None
    return name if (isinstance(name, str) and "/" in name) or name == "UTC" else None


def folder_hash(real_folder: str, case_insensitive: bool = False) -> str:
    return hashlib.sha256(fold_path(real_folder, case_insensitive).rstrip("/").encode("utf-8")).hexdigest()[:16]


@dataclass
class Session:
    folder: str  # the real path of the working folder
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    conversation: str | None = None
    title: str = ""
    model: str | None = None
    incognito: bool = False
    local_only: bool = False
    project: int | None = None
    packs: list[str] | None = None  # None: the server's defaults
    disabled_tools: set[str] = field(default_factory=set)
    todos: TodoStore = field(default_factory=TodoStore)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    # not saved:
    plan_mode: bool = False
    pending_notes: list[str] = field(default_factory=list)  # `!command` output waiting to go with the next message
    context_percent: float = 0.0
    context_tokens: int = 0
    context_window: int = 0
    last_message_id: int | None = None
    tainted: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id, "conversation": self.conversation, "title": self.title, "model": self.model,
            "incognito": self.incognito, "local_only": self.local_only, "project": self.project, "packs": self.packs,
            "disabled_tools": sorted(self.disabled_tools),
            "todos": [{"content": t.content, "status": t.status} for t in self.todos.items],
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "created": self.created, "updated": self.updated,
        }

    @classmethod
    def from_json(cls, folder: str, data: dict[str, Any]) -> Session:
        packs = data.get("packs")
        disabled = data.get("disabled_tools")
        todos = data.get("todos")
        project = data.get("project")
        conversation = data.get("conversation")
        session = cls(
            folder=folder, id=str(data.get("id") or uuid.uuid4().hex[:12]),
            conversation=conversation if isinstance(conversation, str) else None,
            title=str(data.get("title") or ""), model=str(data["model"]) if data.get("model") else None,
            incognito=bool(data.get("incognito")), local_only=bool(data.get("local_only")),
            project=project if isinstance(project, int) else None,
            packs=[str(p) for p in packs] if isinstance(packs, list) else None,
            disabled_tools={str(t) for t in disabled} if isinstance(disabled, list) else set(),
            prompt_tokens=int(data.get("prompt_tokens") or 0), completion_tokens=int(data.get("completion_tokens") or 0),
            created=float(data.get("created") or time.time()), updated=float(data.get("updated") or time.time()))
        if isinstance(todos, list):
            session.todos.items = [Todo(str(t.get("content", "")), str(t.get("status", "pending")))
                                   for t in todos if isinstance(t, dict) and t.get("content")]
        return session

    @property
    def saved_title(self) -> str:
        return self.title or "(untitled)"


class SessionStore:
    def __init__(self, root: Path, real_folder: str, *, case_insensitive: bool = False, keep: int = KEEP_SESSIONS,
                 clock: Callable[[], float] = time.time):
        self.real_folder = real_folder
        self.folder = root / folder_hash(real_folder, case_insensitive)
        self.keep = keep
        self._clock = clock

    def save(self, session: Session) -> bool:
        """Write the session file (not for an incognito session, and not before there is a conversation). Atomic."""
        if session.incognito or session.conversation is None:
            return False
        session.updated = self._clock()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self.folder, prefix=".session-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(session.to_json(), handle)
            os.replace(temporary, self.folder / f"{session.id}.json")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        self.prune()
        return True

    def _files(self) -> list[tuple[float, Path, dict[str, Any]]]:
        found: list[tuple[float, Path, dict[str, Any]]] = []
        try:
            paths = list(self.folder.glob("*.json"))
        except OSError:
            return found
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("conversation"):
                found.append((float(data.get("updated") or 0), path, data))
        return sorted(found, key=lambda item: item[0], reverse=True)

    def list(self) -> list[Session]:
        """This folder's sessions, newest first, at most `keep`."""
        return [Session.from_json(self.real_folder, data) for _, _, data in self._files()[: self.keep]]

    def prune(self) -> int:
        """Remove the files beyond the newest `keep`. Their conversations stay on Clara."""
        extra = self._files()[self.keep:]
        for _, path, _ in extra:
            with contextlib.suppress(OSError):
                path.unlink()
        return len(extra)

    def forget(self, session: Session) -> None:
        with contextlib.suppress(OSError):
            (self.folder / f"{session.id}.json").unlink()
