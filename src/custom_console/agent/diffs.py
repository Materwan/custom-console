"""Unified diffs shown to the user when the agent changes a file."""

from __future__ import annotations

import difflib
from typing import List


def make_diff(old: str, new: str, name: str = "file", context: int = 2) -> str:
    """Unified diff of two texts (without the ``---``/``+++`` header)."""
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=name,
        tofile=name,
        n=context,
        lineterm="",
    )
    return "\n".join(list(lines)[2:])


def new_file_diff(content: str) -> str:
    """The diff of a file that did not exist: every line added."""
    return "\n".join(f"+{line}" for line in content.splitlines())


def clip_diff(diff: str, max_lines: int) -> str:
    """At most `max_lines` lines, with a note telling how many were left out."""
    lines: List[str] = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    hidden = len(lines) - max_lines
    return "\n".join([*lines[:max_lines], f"… {hidden} more line(s)"])


def count_changes(diff: str) -> tuple[int, int]:
    """(added, removed) line counts of a unified diff."""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed
