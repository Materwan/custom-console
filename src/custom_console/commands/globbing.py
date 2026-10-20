"""Expanding `*`, `?`, `[a-z]`, `{a,b}` and `**` in the arguments of commands that take paths.

The matcher is the one shared with the agent's `local_search` (`core/vfs/text.py`), so a pattern means the same everywhere.
Hidden names match only a pattern that starts with a dot. A pattern that matches nothing stays as typed.
"""

from __future__ import annotations

import re

from ..core.vfs.backend import VfsError
from ..core.vfs.ops import Vfs
from ..core.vfs.text import compile_glob

LIMIT = 5000
GLOB_CHARS = re.compile(r"[*?\[{]")


def _split(pattern: str, windows: bool) -> list[str]:
    return re.split(r"[\\/]" if windows else "/", pattern)


def _separator(pattern: str, windows: bool) -> str:
    return "\\" if windows and "\\" in pattern and "/" not in pattern else "/"


def expand(vfs: Vfs, pattern: str, *, windows: bool, ignore_case: bool) -> list[str]:
    """The names a glob matches, as typed (sorted); `[pattern]` itself when it matches nothing."""
    sep = _separator(pattern, windows)
    segments = _split(pattern, windows)
    found = _walk(vfs, segments, 0, "", ignore_case, sep)
    return sorted(dict.fromkeys(found), key=str.lower) or [pattern]


def _walk(vfs: Vfs, segments: list[str], index: int, base: str, ignore_case: bool, sep: str) -> list[str]:
    if index >= len(segments):
        return [base] if base else []
    segment = segments[index]
    last = index == len(segments) - 1
    prefix = base + sep if base and not base.endswith(("/", "\\")) else base
    if index == 0 and segment == "" and len(segments) > 1:  # a rooted pattern: `/src/*.py`
        return _walk(vfs, segments, 1, sep, ignore_case, sep)
    if not GLOB_CHARS.search(segment):
        joined = prefix + segment
        return _walk(vfs, segments, index + 1, joined, ignore_case, sep) if not last else [joined] if _exists(vfs, joined) else []
    results: list[str] = []
    if segment == "**" and not last:  # this folder and every folder below it
        for folder in [base, *_subfolders(vfs, base, prefix, sep)]:
            results += _walk(vfs, segments, index + 1, folder, ignore_case, sep)
            if len(results) >= LIMIT:
                break
        return results
    if segment == "**":  # `src/**` at the end behaves as `src/*`
        segment = "*"
    matcher = compile_glob(segment, ignore_case=ignore_case)
    try:
        entries = vfs.list(vfs.resolve(base or "."))
    except VfsError:
        return []
    for entry in entries:
        if entry.name.startswith(".") and not segment.startswith("."):
            continue
        if not matcher.fullmatch(entry.name):
            continue
        joined = prefix + entry.name
        if last:
            results.append(joined)
        elif entry.is_dir:
            results += _walk(vfs, segments, index + 1, joined, ignore_case, sep)
        if len(results) >= LIMIT:
            break
    return results


def _exists(vfs: Vfs, typed: str) -> bool:
    try:
        return vfs.exists(vfs.resolve(typed))
    except VfsError:
        return False


def _subfolders(vfs: Vfs, base: str, prefix: str, sep: str, depth: int = 6) -> list[str]:
    out: list[str] = []
    try:
        start = vfs.resolve(base or ".")
        for walked in vfs.walk(start, max_depth=depth, include_hidden=False):
            if walked.entry.is_dir and not walked.entry.is_link:
                out.append(prefix + _relative(start.virtual, walked.resolved.virtual).replace("/", sep))
            if len(out) >= LIMIT:
                break
    except VfsError:
        pass
    return out


def _relative(start: str, item: str) -> str:
    base = start.rstrip("/") + "/"
    return item[len(base):] if item.startswith(base) else item
