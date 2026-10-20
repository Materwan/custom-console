"""Read: `cat`, `head`, `tail`, `wc`, `grep`, `diff`.

All of them split text into lines with the one shared function, so `grep -n` and the agent's `local_read` can never disagree
about what line 3 is. Each resolves its path first and judges the *target's* backend afterwards (BUG-15).
"""

from __future__ import annotations

import argparse
import difflib
import re
from collections import deque
from collections.abc import Iterator

from ...core.vfs.backend import VfsError
from ...core.vfs.ops import BinaryFile
from ...core.vfs.resolver import TABLET, Resolved
from ...core.vfs.text import count_lines, decode_text, looks_binary, split_lines
from ..base import CommandError, Context, Line
from ..base import CommandSpec as Spec
from .helpers import DIM, HEADING


def read_file(ctx: Context, typed: str, *, note_read: bool = False) -> tuple[Resolved, str]:
    """The text of a file for a command. Raises CommandError with a plain sentence."""
    target = ctx.vfs.resolve(typed)
    if target.mount == TABLET:
        raise CommandError(f"{typed}: a reMarkable document is not text: copy it out (cp), then use rmdoc2pdf")
    info = ctx.vfs.stat(target)
    if info is None:
        raise CommandError(f"{typed}: No such file or directory")
    if info.is_dir:
        raise CommandError(f"{typed}: Is a folder")
    try:
        file = ctx.vfs.read_text(target)
    except BinaryFile:
        raise CommandError(f"{typed}: binary file") from None
    if file.truncated:
        ctx.shell.err(f"{ctx.name}: {typed}: only the first part of this large file was read")
    if note_read and target.on_disk:
        total = count_lines(file.text)
        ctx.shell.guard.note_read(target.real(), info, first_line=1 if total else 0, last_line=total, total_lines=total,
                                  whole_file=not file.truncated)
    return target, file.text


def _each_input(ctx: Context, paths: list[str], *, note_read: bool = False) -> Iterator[tuple[str, list[str]]]:
    """(label, lines) for each path, or for the piped input when there is no path. A bad path is reported and skipped."""
    if not paths:
        if ctx.has_stdin:
            yield "", [line.plain for line in ctx.stdin_lines()]
        else:
            ctx.fail("no file given, and nothing is piped in")
        return
    for typed in paths:
        try:
            _, text = read_file(ctx, typed, note_read=note_read)
        except (CommandError, VfsError) as error:
            ctx.fail(str(error) if isinstance(error, CommandError) else f"{typed}: {error.message}")
            continue
        yield typed, split_lines(text)


# ---------------------------------------------------------------------- cat
def _cat_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-n", "--number", action="store_true", help="number the lines")
    p.add_argument("paths", nargs="*", help="files to show (default: the piped input)")


def _cat_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    number = 0
    for _, lines in _each_input(ctx, a.paths, note_read=True):
        for line in lines:
            number += 1
            yield Line.of(f"{number:>6}\t{line}") if a.number else Line.of(line)


# ---------------------------------------------------------------------- head, tail
def _count_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("-n", "--lines", type=int, default=10, help="how many lines (default 10)")
    p.add_argument("paths", nargs="*", help="files (default: the piped input)")


def _head_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    if a.paths or not ctx.has_stdin:
        several = len(a.paths) > 1
        for index, (label, lines) in enumerate(_each_input(ctx, a.paths)):
            if several:
                if index:
                    yield Line.of("")
                yield Line.of(f"==> {label} <==", HEADING)
            for line in lines[: max(a.lines, 0)]:
                yield Line.of(line)
        return
    for count, item in enumerate(ctx.stdin_lines()):  # lazily: `cat big | head -n 3` stops early
        if count >= max(a.lines, 0):
            break
        yield item


def _tail_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    keep = max(a.lines, 0)
    if a.paths or not ctx.has_stdin:
        several = len(a.paths) > 1
        for index, (label, lines) in enumerate(_each_input(ctx, a.paths)):
            if several:
                if index:
                    yield Line.of("")
                yield Line.of(f"==> {label} <==", HEADING)
            for line in (lines[-keep:] if keep else []):
                yield Line.of(line)
        return
    window: deque[Line] = deque(maxlen=keep or 1)
    for item in ctx.stdin_lines():
        window.append(item)
    yield from (list(window) if keep else [])


# ---------------------------------------------------------------------- wc
def _wc_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-l", "--lines", action="store_true", help="count lines")
    p.add_argument("-w", "--words", action="store_true", help="count words")
    p.add_argument("-c", "--bytes", action="store_true", help="count bytes")
    p.add_argument("paths", nargs="*", help="files (default: the piped input)")


def _wc_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    chosen = [a.lines, a.words, a.bytes]
    columns = chosen if any(chosen) else [True, True, True]
    totals = [0, 0, 0]
    rows: list[tuple[list[int], str]] = []
    for label, lines in _each_input(ctx, a.paths):
        counts = [len(lines), sum(len(line.split()) for line in lines), sum(len(line.encode("utf-8")) + 1 for line in lines)]
        rows.append((counts, label))
        totals = [t + c for t, c in zip(totals, counts, strict=True)]
    if len(rows) > 1:
        rows.append((totals, "total"))
    for counts, label in rows:
        cells = [f"{c:>7}" for c, wanted in zip(counts, columns, strict=True) if wanted]
        yield Line.of(" ".join(cells) + (f" {label}" if label else ""))


# ---------------------------------------------------------------------- grep
def _grep_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-i", "--ignore-case", action="store_true", help="ignore upper and lower case")
    p.add_argument("-n", "--line-number", action="store_true", help="show line numbers")
    p.add_argument("-r", "--recursive", action="store_true", help="look in folders")
    p.add_argument("-v", "--invert", action="store_true", help="show the lines that do not match")
    p.add_argument("-c", "--count", action="store_true", help="only count the matching lines of each file")
    p.add_argument("pattern", help="a regular expression")
    p.add_argument("paths", nargs="*", help="files or folders (default: the piped input, or . with -r)")


def _grep_files(ctx: Context, a: argparse.Namespace) -> Iterator[tuple[str, list[str]]]:
    if not a.paths and ctx.has_stdin:
        yield "", [line.plain for line in ctx.stdin_lines()]
        return
    for typed in a.paths or (["."] if a.recursive else []):
        try:
            target = ctx.vfs.resolve(typed)
            info = ctx.vfs.stat(target)
            if info is None:
                ctx.fail(f"{typed}: No such file or directory")
                continue
            if info.is_dir:
                if not a.recursive:
                    ctx.fail(f"{typed}: Is a folder (use -r)")
                    continue
                for walked in ctx.vfs.walk(target, on_error=lambda r, e: ctx.fail(f"{r.virtual}: {e.message}")):
                    if walked.entry.is_dir or walked.resolved.mount == TABLET:
                        continue
                    data = ctx.vfs.read_bytes(walked.resolved, 10_000_000)
                    if looks_binary(data):
                        continue
                    rel = walked.resolved.virtual[len(target.virtual.rstrip("/")) + 1:]
                    yield (f"{typed.rstrip('/')}/{rel}" if typed not in (".", "") else rel), split_lines(decode_text(data))
            else:
                _, text = read_file(ctx, typed)
                yield typed, split_lines(text)
        except CommandError as error:
            ctx.fail(str(error))
        except VfsError as error:
            ctx.fail(f"{typed}: {error.message}")
    if not a.paths and not a.recursive and not ctx.has_stdin:
        ctx.fail("no file given, and nothing is piped in", 2)


def _grep_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    try:
        matcher = re.compile(a.pattern, re.I if a.ignore_case else 0)
    except re.error as error:
        ctx.fail(f"invalid regular expression: {error}", 2)
        return
    matched_any = False
    label_files = a.recursive or len(a.paths) > 1
    for label, lines in _grep_files(ctx, a):
        hits = 0
        for number, text in enumerate(lines, 1):
            if bool(matcher.search(text)) == a.invert:
                continue
            hits += 1
            matched_any = True
            if a.count:
                continue
            if ctx.piped_out:
                parts = (f"{label}:" if label_files and label else "") + (f"{number}:" if a.line_number else "") + text
                yield Line.of(parts)
            else:
                pieces: list[tuple[str, str]] = []
                if label_files and label:
                    pieces += [(label, "magenta"), (":", DIM)]
                if a.line_number:
                    pieces += [(str(number), "green"), (":", DIM)]
                yield Line.join(*pieces, (text, ""))
        if a.count:
            yield Line.of(f"{label}:{hits}" if label_files and label else str(hits))
    if not matched_any:
        ctx.status = max(ctx.status, 1)


# ---------------------------------------------------------------------- diff
def _diff_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-U", "--unified", type=int, default=3, help="lines of context (default 3)")
    p.add_argument("first", help="the old file")
    p.add_argument("second", help="the new file")


def _diff_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    first, second = a.first, a.second
    try:
        left, old = read_file(ctx, first)
        right, new = read_file(ctx, second)
    except BinaryFile:
        ctx.status = 1
        yield Line.of("Binary files differ")
        return
    except (CommandError, VfsError) as error:
        ctx.fail(str(error) if isinstance(error, CommandError) else error.message, 2)
        return
    rows = list(difflib.unified_diff(split_lines(old), split_lines(new), left.virtual, right.virtual, n=a.unified, lineterm=""))
    if rows:
        ctx.status = 1
    for row in rows:
        style = "" if ctx.piped_out else ("green" if row.startswith("+") and not row.startswith("+++") else
                                           "red" if row.startswith("-") and not row.startswith("---") else
                                           "cyan" if row.startswith("@@") else "bold" if row.startswith(("+++", "---")) else "")
        yield Line.of(row, style)


COMMANDS = (
    Spec("cat", "Show the text of files (or copy the piped input).", _cat_build, _cat_run, kinds=("file",), globs=True, group="Read"),
    Spec("head", "Show the first lines of files.", lambda p: _count_arg(p), _head_run, kinds=("file",), globs=True, group="Read"),
    Spec("tail", "Show the last lines of files.", lambda p: _count_arg(p), _tail_run, kinds=("file",), globs=True, group="Read"),
    Spec("wc", "Count lines, words and bytes.", _wc_build, _wc_run, kinds=("file",), globs=True, group="Read"),
    Spec("grep", "Show the lines that match a regular expression.", _grep_build, _grep_run, kinds=("none", "file"), globs=True,
         glob_skip=1, group="Read"),
    Spec("diff", "Show the differences between two text files.", _diff_build, _diff_run, kinds=("file", "file"), group="Read"),
)
