"""The overwrite guard: a file may be overwritten only after all of it was read in this session, or if this session
created it. A partial read unlocks nothing (BUG-6), and the same guard serves write, edit, `mv`, `cp` and redirection
(BUG-21), so the rules cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backend import Stat
from .text import fold_path


@dataclass(slots=True)
class _Seen:
    size: int
    mtime: float
    total_lines: int
    covered: list[tuple[int, int]] = field(default_factory=list)  # merged inclusive 1-based line ranges
    whole: bool = False


def _merge(ranges: list[tuple[int, int]], new: tuple[int, int]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted([*ranges, new]):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


class OverwriteGuard:
    def __init__(self, case_insensitive: bool = False):
        self._case_insensitive = case_insensitive
        self._seen: dict[str, _Seen] = {}
        self._created: set[str] = set()

    def key(self, real_path: str) -> str:
        return fold_path(real_path, self._case_insensitive)

    # ------------------------------------------------------------------ notes
    def note_read(self, real_path: str, stat: Stat, *, first_line: int, last_line: int, total_lines: int,
                  whole_file: bool = False) -> None:
        """A read of lines `first_line..last_line` of a file with `total_lines` lines (an empty file: all zero)."""
        key = self.key(real_path)
        seen = self._seen.get(key)
        if seen is None or (seen.size, seen.mtime) != (stat.size, stat.mtime):
            seen = self._seen[key] = _Seen(stat.size, stat.mtime, total_lines)
        if whole_file or total_lines == 0:
            seen.whole = True
        if first_line >= 1 and last_line >= first_line:
            seen.covered = _merge(seen.covered, (first_line, last_line))

    def note_created(self, real_path: str) -> None:
        self._created.add(self.key(real_path))

    def note_written(self, real_path: str, stat: Stat, total_lines: int) -> None:
        """The console wrote the file itself, so it knows the whole content."""
        self._seen[self.key(real_path)] = _Seen(stat.size, stat.mtime, total_lines, [(1, total_lines)] if total_lines else [], True)

    def forget(self, real_path: str) -> None:
        self._seen.pop(self.key(real_path), None)
        self._created.discard(self.key(real_path))

    # ------------------------------------------------------------------ question
    def fully_read(self, real_path: str, stat: Stat) -> bool:
        seen = self._seen.get(self.key(real_path))
        if seen is None or (seen.size, seen.mtime) != (stat.size, stat.mtime):
            return False  # never read, or changed since
        return seen.whole or (seen.total_lines > 0 and seen.covered == [(1, seen.total_lines)])

    def refusal(self, real_path: str, stat: Stat | None, name: str = "") -> str | None:
        """Why this file may not be overwritten now (None: it may). A missing file may always be created."""
        if stat is None:
            return None
        if stat.is_dir:
            return f"{name or real_path} is a folder."
        if self.key(real_path) in self._created and self.fully_known(real_path, stat):
            return None
        if self.fully_read(real_path, stat):
            return None
        shown = name or real_path
        return (f"{shown} exists and has not been read in full in this session (or it changed since): read all of it first, "
                "then write.")

    def fully_known(self, real_path: str, stat: Stat) -> bool:
        """A file this session created and has not seen change since it wrote it."""
        seen = self._seen.get(self.key(real_path))
        return seen is None or (seen.size, seen.mtime) == (stat.size, stat.mtime)
