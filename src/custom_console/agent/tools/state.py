"""Small pieces of state shared by the tools of one agent session."""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
STATUSES = (PENDING, IN_PROGRESS, COMPLETED)
MARKS = {PENDING: "☐", IN_PROGRESS: "◐", COMPLETED: "☑"}


def _mtime(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


@dataclass
class FileRead:
    path: str
    mtime: Optional[int]  # st_mtime_ns when it was read
    complete: bool  # the agent saw the whole file


@dataclass(frozen=True)
class StaleFile:
    """A file that changed since the agent read it."""

    path: str
    modified: Optional[float]  # when it changed (epoch seconds); None: it was deleted
    complete: bool  # the agent had read all of it


class ReadTracker:
    """Remembers which files the agent has seen, and in which state.

    A file may only be edited or overwritten after the agent read it whole, and
    only if it did not change since: otherwise the model would be editing from a
    stale picture of the file. Every read, even partial, is also remembered so
    that the agent can be told when a file changed since (:meth:`stale`).
    """

    def __init__(self) -> None:
        self._seen: Dict[str, Optional[int]] = {}  # whole reads and own writes: what may be edited
        self._reads: Dict[str, FileRead] = {}  # every read

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def mark(self, path: str, complete: bool = True) -> None:
        """The agent knows the file as it is now (it read or wrote it): it may edit it.
        `complete` is False when it read only a range or an outline of it."""
        key, mtime = self._key(path), _mtime(path)
        self._seen[key] = mtime
        self._record(key, path, mtime, complete)

    def saw_part(self, path: str) -> None:
        """The agent read part of the file (a line range, an outline...)."""
        key = self._key(path)
        self._record(key, path, _mtime(path), False)

    def _record(self, key: str, path: str, mtime: Optional[int], complete: bool) -> None:
        # A partial read of an unchanged file the agent had read whole leaves it whole.
        previous = self._reads.get(key)
        if previous is not None and previous.complete and previous.mtime == mtime:
            complete = True
        self._reads[key] = FileRead(os.path.abspath(path), mtime, complete)

    def stale(self) -> List[StaleFile]:
        """The files read by the agent that changed (or disappeared) since, in reading order."""
        changed: List[StaleFile] = []
        for read in self._reads.values():
            mtime = _mtime(read.path)
            if mtime != read.mtime:
                changed.append(StaleFile(read.path, None if mtime is None else mtime / 1e9, read.complete))
        return changed

    def check(self, path: str) -> None:
        """Raise unless `path` was read (or written) by the agent and is unchanged."""
        key = self._key(path)
        if key not in self._seen:
            raise PermissionError(f"{path} has not been read yet: read it with file_system_read first.")
        if self._seen[key] != _mtime(path):
            raise PermissionError(f"{path} changed since it was read: read it again first.")

    def clear(self) -> None:
        self._seen.clear()
        self._reads.clear()


@dataclass
class TodoItem:
    content: str
    status: str = PENDING


class TodoList:
    """The agent's plan for the task at hand (replaced as a whole on each update)."""

    def __init__(self) -> None:
        self.items: List[TodoItem] = []

    def replace(self, raw: List[Any]) -> None:
        items: List[TodoItem] = []
        for entry in raw:
            if isinstance(entry, str):
                items.append(TodoItem(entry.strip()))
                continue
            if not isinstance(entry, dict):
                raise ValueError(f"Each todo must be an object with 'content' and 'status', got {entry!r}.")
            content = str(entry.get("content") or entry.get("task") or entry.get("title") or "").strip()
            status = str(entry.get("status") or PENDING).strip().lower().replace(" ", "_").replace("-", "_")
            if status not in STATUSES:
                raise ValueError(f"Invalid status {status!r}: use one of {', '.join(STATUSES)}.")
            items.append(TodoItem(content, status))
        if any(not item.content for item in items):
            raise ValueError("Every todo needs a non-empty 'content'.")
        if sum(item.status == IN_PROGRESS for item in items) > 1:
            raise ValueError("Only one todo may be in_progress at a time.")
        self.items = items

    def to_list(self) -> List[Dict[str, str]]:
        """The plan as plain data (what ``replace`` accepts), for saved sessions."""
        return [{"content": item.content, "status": item.status} for item in self.items]

    def render(self) -> str:
        return "\n".join(f"{MARKS[item.status]} {item.content}" for item in self.items)

    def summary(self) -> str:
        done = sum(item.status == COMPLETED for item in self.items)
        return f"{done}/{len(self.items)} done"

    def progress(self) -> Tuple[int, int, str]:
        """(items done, items, the item in progress or "")."""
        done = sum(item.status == COMPLETED for item in self.items)
        active = next((item.content for item in self.items if item.status == IN_PROGRESS), "")
        return done, len(self.items), active

    def clear(self) -> None:
        self.items = []
