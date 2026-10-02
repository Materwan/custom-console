"""Snapshots of the files the agent changes, so that ``/undo`` can restore them.

Changes are grouped per turn. Before a file or folder is modified, deleted or
overwritten its previous content is copied into the checkpoints directory; a
path that did not exist before is remembered as *created*, and a move is
remembered so that it can be reversed. Changes made through ``run_command``
cannot be tracked.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
KEEP_DAYS = 7

RESTORE = "restore"  # the path existed: put the saved copy back
CREATED = "created"  # the path is new: delete it
MOVE = "move"  # `path` was moved here from `origin`: move it back


class SnapshotTooLargeError(OSError):
    """The content to protect is bigger than the snapshot budget."""


@dataclass
class Change:
    kind: str
    path: Path
    backup: Optional[Path] = None
    origin: Optional[Path] = None


@dataclass
class Group:
    label: str
    changes: List[Change] = field(default_factory=list)

    def touches(self, path: Path) -> bool:
        return any(change.path == path for change in self.changes)


def _size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if total > MAX_SNAPSHOT_BYTES:
                return total
    return total


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


class Checkpoints:
    def __init__(self, directory: Path, *, limit: int = MAX_SNAPSHOT_BYTES):
        self.directory = Path(directory) / time.strftime("%Y%m%d-%H%M%S")
        self.limit = limit
        self._groups: List[Group] = []
        self._counter = 0
        self._prune(Path(directory))

    @staticmethod
    def _prune(root: Path) -> None:
        """Forget the snapshots of old sessions."""
        limit = time.time() - KEEP_DAYS * 86400
        try:
            for entry in root.iterdir():
                if entry.is_dir() and entry.stat().st_mtime < limit:
                    shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass

    # -- recording ----------------------------------------------------------- #

    def begin_turn(self, label: str) -> None:
        if not self._groups or self._groups[-1].changes:
            self._groups.append(Group(label))
        else:
            self._groups[-1].label = label

    @property
    def _group(self) -> Group:
        if not self._groups:
            self._groups.append(Group("changes"))
        return self._groups[-1]

    def backup(self, path: "str | os.PathLike[str]") -> None:
        """Call before `path` is modified, overwritten or deleted."""
        target = Path(path)
        group = self._group
        if group.touches(target):
            return  # the state before the turn is already saved
        if not (target.exists() or target.is_symlink()):
            group.changes.append(Change(CREATED, target))
            return
        if _size(target) > self.limit:
            raise SnapshotTooLargeError(
                f"{target} is larger than {self.limit // (1024 * 1024)} MB: "
                "refusing to change it without being able to undo"
            )
        self._counter += 1
        saved = self.directory / str(self._counter) / target.name
        saved.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir() and not target.is_symlink():
            shutil.copytree(target, saved, symlinks=True)
        else:
            shutil.copy2(target, saved, follow_symlinks=False)
        group.changes.append(Change(RESTORE, target, backup=saved))

    def moved(self, origin: "str | os.PathLike[str]", destination: "str | os.PathLike[str]") -> None:
        self._group.changes.append(Change(MOVE, Path(destination), origin=Path(origin)))

    # -- undoing -------------------------------------------------------------- #

    @property
    def available(self) -> int:
        """How many turns with file changes can be undone."""
        return sum(1 for group in self._groups if group.changes)

    def undo(self) -> List[str]:
        """Revert the most recent turn that changed files. Returns one line per
        file; raises LookupError when there is nothing to undo."""
        while self._groups and not self._groups[-1].changes:
            self._groups.pop()
        if not self._groups:
            raise LookupError("nothing to undo")

        group = self._groups.pop()
        report: List[str] = []
        for change in reversed(group.changes):
            try:
                report.append(self._revert(change))
            except OSError as error:
                report.append(f"could not restore {change.path}: {error}")
        return report

    @staticmethod
    def _revert(change: Change) -> str:
        path = change.path
        if change.kind == CREATED:
            _delete(path)
            return f"removed {path}"
        if change.kind == MOVE:
            assert change.origin is not None
            if not path.exists():
                return f"{path} is gone, cannot move it back"
            if change.origin.exists():
                return f"{change.origin} exists again, left {path} in place"
            change.origin.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(path, change.origin)
            return f"moved {path} back to {change.origin}"
        assert change.backup is not None
        _delete(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if change.backup.is_dir() and not change.backup.is_symlink():
            shutil.copytree(change.backup, path, symlinks=True)
        else:
            shutil.copy2(change.backup, path, follow_symlinks=False)
        shutil.rmtree(change.backup.parent, ignore_errors=True)
        return f"restored {path}"
