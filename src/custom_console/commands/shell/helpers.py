"""Small things the shell commands share."""

from __future__ import annotations

import re
import time

from ...core.vfs.backend import Entry
from ..base import Line

DIR_STYLE = "bold blue"
LINK_STYLE = "cyan"
DIM = "dim"
HEADING = "yellow"
NEEDS_QUOTES = re.compile(r"[\s'\"*?\[\]{}|<>&;$`\\]")


def quote(name: str) -> str:
    """A name as a person would have to type it: in double quotes when it has spaces or shell characters."""
    if not name or not NEEDS_QUOTES.search(name):
        return name
    return '"' + name.replace('"', '\\"') + '"'


def when(mtime: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "-"


def size_text(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{int(value)}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return str(size)


def entry_line(entry: Entry, *, quoted: bool = True, suffix: bool = True) -> Line:
    """A name coloured for the screen: folders blue with a trailing slash, links cyan."""
    shown = quote(entry.name) if quoted else entry.name
    if entry.is_dir:
        return Line.of(shown + ("/" if suffix else ""), DIR_STYLE)
    return Line.of(shown, LINK_STYLE if entry.is_link else "")


def wrap_names(lines: list[Line], width: int) -> list[Line]:
    """Names on as few lines as fit `width`, two spaces apart."""
    rows: list[Line] = []
    current: list[tuple[str, str]] = []
    used = 0
    for item in lines:
        size = len(item.plain)
        if current and used + 2 + size > width:
            rows.append(Line(tuple(current)))
            current, used = [], 0
        if current:
            current.append(("  ", ""))
            used += 2
        current.extend(item.segments)
        used += size
    if current:
        rows.append(Line(tuple(current)))
    return rows


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
