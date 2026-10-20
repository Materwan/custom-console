"""Diffs for permission prompts and edit results, and the "closest passage" hint for an edit that does not match.

Both are bounded: a preview stops after a set number of rows and ends with "... N more lines" (N4), and the closest-passage
search has a time budget and anchors on lines that really occur in the file (PERF-2).
"""

from __future__ import annotations

import difflib
import time

MAX_PREVIEW_ROWS = 40


def unified(old: str, new: str, name: str = "", *, context: int = 3, max_rows: int = MAX_PREVIEW_ROWS) -> str:
    """A unified diff, cut after `max_rows` rows."""
    rows = list(difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{name}" if name else "before",
                                     f"b/{name}" if name else "after", n=context, lineterm=""))
    return cap_rows(rows, max_rows)


def cap_rows(rows: list[str], max_rows: int = MAX_PREVIEW_ROWS) -> str:
    if len(rows) <= max_rows:
        return "\n".join(rows)
    return "\n".join([*rows[:max_rows], f"... {len(rows) - max_rows} more lines"])


def new_file_preview(text: str, max_rows: int = MAX_PREVIEW_ROWS) -> str:
    rows = [f"+{line}" for line in text.split("\n")]
    if rows and rows[-1] == "+":
        rows.pop()
    return cap_rows(rows, max_rows)


def stats(old: str, new: str) -> tuple[int, int]:
    """(lines added, lines removed)."""
    added = removed = 0
    for row in difflib.ndiff(old.split("\n"), new.split("\n")) if len(old) + len(new) < 200_000 else ():
        added += row.startswith("+ ")
        removed += row.startswith("- ")
    return added, removed


def closest_passage(text: str, needle: str, *, budget: float = 0.5, max_lines: int = 20_000) -> tuple[int, str] | None:
    """The place in `text` that looks most like `needle` (line number and a few lines), or None. Gives up after `budget`
    seconds, looks at no more than `max_lines` lines, and starts only from lines that occur in the text."""
    deadline = time.monotonic() + budget
    lines = text.split("\n")[:max_lines]
    wanted = [line.strip() for line in needle.split("\n")]
    anchor = next((line for line in wanted if line), "")
    if not anchor:
        return None
    stripped = [line.strip() for line in lines]
    starts = [i for i, line in enumerate(stripped) if line == anchor]
    if not starts:
        pool = list(dict.fromkeys(line for line in stripped if line))[:5000]
        close = difflib.get_close_matches(anchor, pool, n=3, cutoff=0.75)
        starts = [i for i, line in enumerate(stripped) if line in close]
    best: tuple[float, int] | None = None
    width = max(len(wanted), 1)
    target = "\n".join(wanted)
    for start in starts[:50]:
        if time.monotonic() > deadline:
            break
        window = "\n".join(stripped[start:start + width])
        ratio = difflib.SequenceMatcher(None, window, target, autojunk=False).quick_ratio()
        if ratio >= 0.5 and (best is None or ratio > best[0]):
            best = (ratio, start)
    if best is None:
        return None
    start = best[1]
    return start + 1, "\n".join(lines[start:start + width][:12])
