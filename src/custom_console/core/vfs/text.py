"""Text helpers shared by the shell and the tools: one way to split and number lines, one glob matcher.

Having one of each is the fix for BUG-12 to 14: `read`, `grep`, `head`, `tail` and `wc` can no longer disagree about
what line 3 is, and a `{a,b}` glob means the same everywhere.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

SNIFF = 8192  # bytes looked at to tell binary from text
MAX_BRACE_EXPANSION = 256


def fold_path(path: str, case_insensitive: bool) -> str:
    """A path as a comparison key: `/` separators, and lower case where the file system ignores case."""
    path = path.replace("\\", "/")
    return path.lower() if case_insensitive else path


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:SNIFF]


def decode_text(data: bytes) -> str:
    """UTF-8 (a BOM is dropped, bad bytes become U+FFFD), with every line ending turned into `\\n`."""
    text = data.decode("utf-8", errors="replace").removeprefix("﻿")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    """The lines of a text: `"a\\nb\\n"` is two lines, `"a\\nb"` is two, `""` is none, `"\\n"` is one empty line."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def count_lines(text: str) -> int:
    return len(split_lines(text))


def number_lines(lines: Sequence[str], start: int = 1, width: int | None = None) -> str:
    """`    12\\tthe line`, numbered from `start` (1-based), the way every tool shows lines."""
    width = width if width is not None else max(len(str(start + len(lines) - 1)), 1)
    return "\n".join(f"{start + i:>{width}}\t{line}" for i, line in enumerate(lines))


@dataclass(frozen=True, slots=True)
class LineWindow:
    lines: list[str]
    first: int  # 1-based number of the first line shown (0 when none)
    last: int
    total: int

    @property
    def whole(self) -> bool:
        return self.first <= 1 and self.last >= self.total

    @property
    def more_after(self) -> int:
        return max(self.total - self.last, 0)


def line_window(text: str, offset: int = 1, limit: int | None = None) -> LineWindow:
    """Lines `offset` to `offset + limit - 1` (1-based, `limit` None: to the end)."""
    lines = split_lines(text)
    total = len(lines)
    start = max(offset, 1)
    end = total if limit is None else min(total, start - 1 + max(limit, 0))
    shown = lines[start - 1:end]
    return LineWindow(shown, start if shown else 0, start - 1 + len(shown) if shown else 0, total)


# ---------------------------------------------------------------------- globs
def expand_braces(pattern: str) -> list[str]:
    """`a.{py,md}` -> `a.py`, `a.md`; nested braces work; a brace with no comma stays as it is."""
    out: list[str] = []

    def expand(text: str) -> None:
        if len(out) >= MAX_BRACE_EXPANSION:
            return
        depth = 0
        start = -1
        for index, char in enumerate(text):
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}" and depth:
                depth -= 1
                if depth == 0:
                    options, level, begin = [], 0, start + 1
                    for j in range(start + 1, index):
                        if text[j] == "{":
                            level += 1
                        elif text[j] == "}":
                            level -= 1
                        elif text[j] == "," and level == 0:
                            options.append(text[begin:j])
                            begin = j + 1
                    options.append(text[begin:index])
                    if len(options) > 1:
                        for option in options:
                            expand(text[:start] + option + text[index + 1:])
                        return
        out.append(text)

    expand(pattern)
    return out


def _glob_regex(pattern: str) -> str:
    result, i, n = [], 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 3] == "**/":
                result.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                result.append(".*")
                i += 2
                continue
            result.append("[^/]*")
        elif char == "?":
            result.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                result.append(re.escape(char))
            else:
                body = pattern[i + 1:end]
                negate = body.startswith(("!", "^"))
                body = body[1:] if negate else body
                result.append("[" + ("^" if negate else "") + body.replace("\\", "\\\\") + "]")
                i = end
        else:
            result.append(re.escape(char))
        i += 1
    return "".join(result)


def compile_glob(pattern: str, *, ignore_case: bool = False) -> re.Pattern[str]:
    """A glob as a regex over a `/`-separated path: `*`, `?`, `[a-z]`, `**`, `**/` and `{a,b}`."""
    alternatives = "|".join(_glob_regex(p) for p in expand_braces(pattern))
    return re.compile(f"(?:{alternatives})", re.I | re.S if ignore_case else re.S)


def glob_match(pattern: str, path: str, *, ignore_case: bool = False) -> bool:
    """Whether `path` (with `/` separators) matches. A pattern with no `/` is matched against the name alone."""
    path = path.replace("\\", "/")
    target = path.rsplit("/", 1)[-1] if "/" not in pattern else path.lstrip("/")
    return compile_glob(pattern.lstrip("/") if "/" in pattern else pattern, ignore_case=ignore_case).fullmatch(target) is not None
