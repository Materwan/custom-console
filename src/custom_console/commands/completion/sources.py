"""Where completion candidates come from: command names, flags, and the entries of a folder."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from ...core.vfs.backend import Entry

NEEDS_QUOTES = re.compile(r"[\s'\"*?\[\]{}|<>&;$`#~]")
EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".com", ".ps1")
MAX_CANDIDATES = 500


@dataclass(frozen=True, slots=True)
class Candidate:
    text: str  # what replaces the word being completed, already quoted
    display: str  # what the menu shows
    kind: str  # command, alias, flag, dir, file, app, history, mount, value
    meta: str = ""  # a few words of help, shown next to it


@dataclass(frozen=True, slots=True)
class Completions:
    start: int  # where in the line the replaced word begins
    candidates: tuple[Candidate, ...] = ()
    message: str = ""  # something to tell the person instead of a menu (a slow tablet gave up)

    def __bool__(self) -> bool:
        return bool(self.candidates)


def quote_for_insert(value: str, open_quote: str, is_dir: bool, windows: bool) -> str:
    """`value` as it must be typed. Inside an open quote the quote is kept (and closed for a file, left open for a folder so
    the next Tab goes on inside it); outside one, a name with spaces or shell characters gets double quotes."""
    if open_quote:
        return open_quote + value + ("" if is_dir else open_quote)
    if NEEDS_QUOTES.search(value) and not (value.startswith("~") and not NEEDS_QUOTES.search(value[1:])):
        escaped = value if windows else value.replace('"', '\\"')
        return '"' + escaped + ("" if is_dir else '"')
    return value


def matches(name: str, prefix: str, ignore_case: bool) -> bool:
    return name.lower().startswith(prefix.lower()) if ignore_case else name.startswith(prefix)


def path_executables(path_variable: str, windows: bool) -> list[str]:
    """Names of the programs on the PATH (without their extension on Windows), each once."""
    found: dict[str, None] = {}
    for folder in path_variable.split(os.pathsep):
        try:
            with os.scandir(folder) as scan:
                for item in scan:
                    name = item.name
                    if windows:
                        stem, ext = os.path.splitext(name)
                        if ext.lower() in EXECUTABLE_SUFFIXES:
                            found[stem.lower()] = None
                    elif item.is_file() and os.access(item.path, os.X_OK):
                        found[name] = None
        except OSError:
            continue
    return sorted(found)


def folder_candidates(entries: Iterable[Entry], prefix: str, kind: str, ignore_case: bool) -> list[Entry]:
    """The entries of a folder that fit what is typed: hidden ones only when the typed part starts with a dot, folders only for
    a `dir` argument, folders first."""
    out = []
    for entry in entries:
        if not matches(entry.name, prefix, ignore_case):
            continue
        if entry.hidden and not prefix.startswith("."):
            continue
        if kind == "dir" and not entry.is_dir:
            continue
        out.append(entry)
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out[:MAX_CANDIDATES]
