"""`/undo`: put back the files the console changed during the last turn.

1. Before each `local_write` or `local_edit`, the original is copied to `<data>/undo/<turn>/` and a record notes its path,
   its mode, and whether it existed at all. After the write, the record gets the hash of what was written.
2. `/undo` goes back through the last turn's records in reverse. A file that someone else changed since the console wrote it
   (its hash is no longer the recorded one) is skipped with a warning, never overwritten.
3. A restore is written to a temporary file in the same folder and swapped in with `os.replace`, so a crash cannot leave a
   half-written file. A file the console created is deleted only after the same check.
4. A record is removed only after its file is restored. A failure keeps it and names the file.
5. Only the last 20 turns are kept.

`local_run` changes cannot be undone: its permission prompt says so. There is no move tool, so there are no move records.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

KEEP_TURNS = 20


def file_hash(path: str | Path) -> str | None:
    """sha256 of a file's bytes; None when it does not exist (or cannot be read)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class UndoRecord:
    path: str  # the real path
    existed: bool
    backup: str = ""  # name of the copy of the original inside the turn's folder
    mode: int | None = None
    after: str | None = None  # hash of what the console wrote; None: the write did not finish
    created_dirs: list[str] = field(default_factory=list)  # folders the write had to create, outermost first

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "existed": self.existed, "backup": self.backup, "mode": self.mode, "after": self.after,
                "created_dirs": self.created_dirs}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> UndoRecord:
        mode = data.get("mode")
        after = data.get("after")
        dirs = data.get("created_dirs")
        return cls(str(data["path"]), bool(data.get("existed")), str(data.get("backup", "")),
                   mode if isinstance(mode, int) else None, after if isinstance(after, str) else None,
                   [str(d) for d in dirs] if isinstance(dirs, list) else [])


@dataclass(slots=True)
class UndoReport:
    turn: str = ""
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # already as they were: nothing to do
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why): left alone, the record is kept
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, why): could not restore, the record is kept

    @property
    def nothing(self) -> bool:
        return not (self.restored or self.deleted or self.unchanged or self.skipped or self.failed)

    @property
    def clean(self) -> bool:
        return not (self.skipped or self.failed)


class UndoLog:
    def __init__(self, root: Path, keep_turns: int = KEEP_TURNS, clock: Callable[[], float] = time.time):
        self.root = root
        self.keep_turns = keep_turns
        self._clock = clock
        self._turn: Path | None = None
        self._records: list[UndoRecord] = []

    # ------------------------------------------------------------------ recording
    def begin_turn(self, turn_id: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", turn_id)[:60] or "turn"
        self._turn = self.root / f"{time.time_ns():020d}-{safe}"
        self._records = []

    def _folder(self) -> Path:
        if self._turn is None:
            self.begin_turn("turn")
        assert self._turn is not None
        return self._turn

    def _save(self) -> None:
        folder = self._folder()
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".records-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"records": [r.to_json() for r in self._records if r.path]}, handle)
        os.replace(temporary, folder / "records.json")

    def before(self, path: str, created_dirs: list[str] | None = None) -> int:
        """Call before changing `path` (its real path). Copies the original away. Returns the record's number."""
        folder = self._folder()
        folder.mkdir(parents=True, exist_ok=True)
        number = len(self._records)
        record = UndoRecord(path, False, created_dirs=list(created_dirs or []))
        try:
            info = os.stat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"{path} is not a regular file")
            record.existed = True
            record.mode = stat.S_IMODE(info.st_mode)
            record.backup = f"{number}.bak"
            shutil.copyfile(path, folder / record.backup)
        self._records.append(record)
        self._save()
        return number

    def after(self, number: int, data: bytes) -> None:
        """Call after the write: remembers what the file should hold now."""
        self._records[number].after = bytes_hash(data)
        self._save()

    def discard(self, number: int) -> None:
        """The write failed before changing anything: forget the record."""
        record = self._records[number]
        if record.backup:
            with contextlib.suppress(OSError):
                (self._folder() / record.backup).unlink()
        self._records[number] = UndoRecord("", False)  # keeps the numbers of the others stable; it is not saved
        self._save()

    @property
    def pending_changes(self) -> int:
        return sum(1 for r in self._records if r.path)

    # ------------------------------------------------------------------ reading back
    def _turn_folders(self) -> list[Path]:
        try:
            return sorted(p for p in self.root.iterdir() if p.is_dir() and (p / "records.json").exists())
        except OSError:
            return []

    @staticmethod
    def _load(folder: Path) -> list[UndoRecord]:
        try:
            data = json.loads((folder / "records.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("records") if isinstance(data, dict) else None
        return [UndoRecord.from_json(i) for i in items if isinstance(i, dict) and i.get("path")] if isinstance(items, list) else []

    def last_turn_records(self) -> list[UndoRecord]:
        folders = self._turn_folders()
        return self._load(folders[-1]) if folders else []

    # ------------------------------------------------------------------ undoing
    def undo_last(self) -> UndoReport:
        folder, records = None, []
        for candidate in reversed(self._turn_folders()):
            records = self._load(candidate)
            if records:
                folder = candidate
                break
            shutil.rmtree(candidate, ignore_errors=True)  # a turn whose records were all dropped
        if folder is None:
            return UndoReport()
        report = UndoReport(turn=folder.name.split("-", 1)[-1])
        kept: list[UndoRecord] = []
        for record in reversed(records):
            outcome = self._undo_one(folder, record)
            if outcome == "restored":
                report.restored.append(record.path)
            elif outcome == "deleted":
                report.deleted.append(record.path)
            elif outcome == "gone":
                report.unchanged.append(record.path)
            elif outcome.startswith("skip:"):
                report.skipped.append((record.path, outcome[5:]))
                kept.append(record)
            else:
                report.failed.append((record.path, outcome[5:]))
                kept.append(record)
        kept.reverse()
        if kept:
            try:
                self._write_records(folder, kept)
            except OSError as error:  # the old records stay: a superset, and the hash check keeps a re-run safe
                report.failed.append((str(folder), f"could not update the undo records ({error.strerror or error})"))
        else:
            shutil.rmtree(folder, ignore_errors=True)
        if folder == self._turn:
            self._records = kept
        return report

    def _write_records(self, folder: Path, records: list[UndoRecord]) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".records-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"records": [r.to_json() for r in records]}, handle)
        os.replace(temporary, folder / "records.json")

    def _undo_one(self, folder: Path, record: UndoRecord) -> str:
        current = file_hash(record.path)
        original = file_hash(folder / record.backup) if record.existed else None
        if record.after is None:  # the write did not finish: only act if the file is still exactly the original
            if record.existed and current == original:
                return "gone"
            if not record.existed and current is None:
                return "gone"
            return "skip:the write did not finish and the file is not as it was; check it yourself"
        if current != record.after:
            if current is None:
                return "skip:the file was removed since the console wrote it"
            return "skip:the file changed since the console wrote it, so it was left alone"
        try:
            if record.existed:
                if original is None:
                    return "fail:the saved copy of the original is missing"
                self._restore(record, folder / record.backup)
                return "restored"
            os.unlink(record.path)
            for created in reversed(record.created_dirs):
                with contextlib.suppress(OSError):
                    os.rmdir(created)  # only if the console made it and it is empty
            return "deleted"
        except OSError as error:
            return f"fail:{error.strerror or error}"

    @staticmethod
    def _restore(record: UndoRecord, backup: Path) -> None:
        target_folder = os.path.dirname(os.path.abspath(record.path))
        descriptor, temporary = tempfile.mkstemp(dir=target_folder, prefix=".undo-")
        try:
            with os.fdopen(descriptor, "wb") as out, open(backup, "rb") as source:
                shutil.copyfileobj(source, out)
                out.flush()
                os.fsync(out.fileno())
            if record.mode is not None:
                os.chmod(temporary, record.mode)
            os.replace(temporary, record.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    # ------------------------------------------------------------------ housekeeping
    def prune(self) -> int:
        """Keep the newest `keep_turns` turns. Returns how many were removed."""
        folders = self._turn_folders()
        old = folders[: max(len(folders) - self.keep_turns, 0)]
        for folder in old:
            shutil.rmtree(folder, ignore_errors=True)
        return len(old)
