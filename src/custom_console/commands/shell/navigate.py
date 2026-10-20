"""Navigate: `cd`, `pwd`, `ls`, `tree`, `stat`, `find`."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator

from ...core.vfs.backend import Entry, VfsError
from ...core.vfs.resolver import Resolved
from ..base import CommandSpec, Context, Line
from .helpers import DIM, DIR_STYLE, HEADING, entry_line, size_text, when, wrap_names


# ---------------------------------------------------------------------- cd, pwd
def _cd_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", help="folder to go to (default: your home folder; '-' is the previous folder)")


def _cd_run(ctx: Context, a: argparse.Namespace) -> None:
    shell = ctx.shell
    if a.path == "-":
        if shell.previous_dir is None:
            ctx.fail("no previous folder")
            return
        typed = shell.previous_dir
    else:
        typed = a.path or "~"
    before = shell.vfs.cwd
    try:
        shell.vfs.chdir(typed)
    except VfsError as error:
        ctx.fail(f"{typed}: {error.message}")
        return
    shell.previous_dir = before
    return None


def _pwd_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-P", "--physical", action="store_true", help="show the real path, links resolved")


def _pwd_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    here = ctx.vfs.here()
    yield Line.of(here.real() if a.physical and here.on_disk else here.virtual)


# ---------------------------------------------------------------------- ls
def _ls_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-a", "--all", action="store_true", help="include hidden entries (names starting with a dot)")
    p.add_argument("-l", "--long", action="store_true", help="one entry per line with its size and date")
    p.add_argument("-1", dest="one", action="store_true", help="one entry per line")
    p.add_argument("paths", nargs="*", help="what to list (default: the current folder)")


def _ls_layout(ctx: Context, a: argparse.Namespace, entries: list[Entry]) -> Iterator[Line]:
    if ctx.piped_out:
        for entry in entries:
            yield Line.of(entry.name)
    elif a.long:
        for entry in entries:
            kind = "d" if entry.is_dir else "l" if entry.is_link else "-"
            yield Line.join((f"{kind} {size_text(entry.size):>8}  {when(entry.mtime)}  ", DIM), *entry_line(entry).segments)
    elif a.one:
        for entry in entries:
            yield entry_line(entry)
    else:
        yield from wrap_names([entry_line(e) for e in entries], max(ctx.shell.width(), 20))


def _ls_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    vfs = ctx.vfs
    targets = a.paths or ["."]
    files: list[Entry] = []
    folders: list[tuple[str, list[Entry]]] = []
    for typed in targets:
        try:
            resolved = vfs.resolve(typed)
            info = vfs.stat(resolved)
            if info is None:
                ctx.fail(f"cannot access '{typed}': No such file or directory")
                continue
            if info.is_dir:
                entries = vfs.list(resolved)
                if not a.all:
                    entries = [e for e in entries if not e.hidden]
                entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
                folders.append((typed, entries))
            else:
                files.append(Entry(typed.rsplit("/", 1)[-1], False, info.size, info.mtime))
        except VfsError as error:
            ctx.fail(f"cannot access '{typed}': {error.message}")
    files.sort(key=lambda e: e.name.lower())
    first = True
    if files:  # files named on the command line come first, together, with no heading
        yield from _ls_layout(ctx, a, files)
        first = False
    for typed, entries in folders:
        if len(targets) > 1:  # GNU ls: a heading per folder when more than one thing was named
            if not first:
                yield Line.of("")
            yield Line.of(f"{typed}:", HEADING)
        first = False
        yield from _ls_layout(ctx, a, entries)


# ---------------------------------------------------------------------- tree
def _tree_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-a", "--all", action="store_true", help="include hidden entries")
    p.add_argument("-d", "--depth", type=int, default=3, help="how deep to go (default 3, -1 for no limit)")
    p.add_argument("paths", nargs="*", help="where to start (default: the current folder)")


def _tree_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    vfs = ctx.vfs
    chars = ("├── ", "└── ", "│   ", "    ") if ctx.shell.unicode else ("|-- ", "`-- ", "|   ", "    ")
    totals = [0, 0]
    limit = None if a.depth < 0 else a.depth

    def children(folder: Resolved, depth: int, prefix: str) -> Iterator[Line]:
        try:
            entries = vfs.list(folder)
        except VfsError as error:
            yield Line.join((prefix + chars[1], ""), (f"({error.message.lower()})", DIM))
            return
        if not a.all:
            entries = [e for e in entries if not e.hidden]
        entries.sort(key=lambda e: (e.is_dir, e.name.lower()))  # files first, then folders
        if limit is not None and depth > limit:
            if entries:
                yield Line.of(prefix + chars[1] + "...", DIM)
            return
        for index, entry in enumerate(entries):
            ctx.shell.cancel.raise_if_cancelled()
            last = index == len(entries) - 1
            yield Line.join((prefix + (chars[1] if last else chars[0]), ""), *entry_line(entry).segments)
            if entry.is_dir:
                totals[0] += 1
                if not entry.is_link:
                    yield from children(vfs.child(folder, entry.name), depth + 1, prefix + (chars[3] if last else chars[2]))
            else:
                totals[1] += 1

    for typed in a.paths or ["."]:
        try:
            start = vfs.resolve(typed)
            info = vfs.stat(start)
        except VfsError as error:
            ctx.fail(f"{typed}: {error.message}")
            continue
        if info is None:
            ctx.fail(f"{typed}: No such file or directory")
            continue
        yield Line.of(start.virtual, DIR_STYLE)
        if info.is_dir:
            yield from children(start, 1, "")
    yield Line.of(f"{totals[0]} folders, {totals[1]} files", DIM)


# ---------------------------------------------------------------------- stat
def _stat_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="*", help="what to look at (default: the current folder)")


def _stat_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    for typed in a.paths or ["."]:
        try:
            resolved = ctx.vfs.resolve(typed)
            info = ctx.vfs.stat(resolved)
        except VfsError as error:
            ctx.fail(f"{typed}: {error.message}")
            continue
        if info is None:
            ctx.fail(f"{typed}: No such file or directory")
            continue
        kind = "link to " if info.is_link else ""
        kind += "folder" if info.is_dir else "file"
        flags = ("r" if info.readable else "-") + ("w" if info.writable else "-") + ("x" if info.executable else "-")
        yield Line.of(f"  Path: {resolved.virtual}")
        yield Line.of(f"  Type: {kind}")
        if not info.is_dir:
            yield Line.of(f"  Size: {info.size:,} bytes")
        if info.mtime:
            yield Line.of(f"  Modified: {when(info.mtime)}")
        yield Line.of(f"  Access: {flags}")


# ---------------------------------------------------------------------- find
def _find_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("pattern", help="a regular expression matched against names (end with / for folders only)")
    p.add_argument("path", nargs="?", default=".", help="where to look (default: the current folder)")
    p.add_argument("-d", "--depth", type=int, default=5, help="how deep to go (default 5, -1 for no limit)")
    p.add_argument("-s", "--strict", action="store_true", help="match upper and lower case exactly")


def _find_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    folders_only = a.pattern.endswith("/")
    text = a.pattern.rstrip("/") or a.pattern
    flags = 0 if a.strict else re.I
    try:
        matcher = re.compile(text, flags)
    except re.error:
        matcher = re.compile(re.escape(text), flags)  # an invalid expression is searched literally
    vfs = ctx.vfs
    try:
        start = vfs.resolve(a.path)
        if not vfs.is_dir(start):
            ctx.fail(f"{a.path}: not a folder")
            return
    except VfsError as error:
        ctx.fail(f"{a.path}: {error.message}")
        return
    base = a.path.rstrip("/\\") if a.path not in (".", "") else ""
    found = 0
    skipped: list[str] = []
    for walked in vfs.walk(start, max_depth=None if a.depth < 0 else a.depth, on_error=lambda r, e: skipped.append(r.virtual)):
        if folders_only and not walked.entry.is_dir:
            continue
        if matcher.search(walked.entry.name):
            found += 1
            relative = walked.resolved.virtual[len(start.virtual.rstrip("/")) + 1:]
            shown = f"{base}/{relative}" if base else relative
            if ctx.piped_out:
                yield Line.of(shown)
            else:
                yield entry_line(Entry(shown, walked.entry.is_dir, is_link=walked.entry.is_link))
    if not found and not ctx.piped_out:
        ctx.shell.err("find: nothing matches")
    for place in skipped:
        ctx.shell.err(f"find: could not read {place}")


COMMANDS = (
    CommandSpec("cd", "Change the current folder (also onto a drive, a WSL distribution or the reMarkable).", _cd_build, _cd_run,
                kinds=("dir",), group="Navigate"),
    CommandSpec("pwd", "Print the current location.", _pwd_build, _pwd_run, kinds=("none",), group="Navigate"),
    CommandSpec("ls", "List folders and files.", _ls_build, _ls_run, kinds=("any",), group="Navigate", globs=True),
    CommandSpec("tree", "Show a folder as a tree (files first, then folders).", _tree_build, _tree_run, kinds=("dir",),
                group="Navigate"),
    CommandSpec("stat", "Show details of files and folders.", _stat_build, _stat_run, kinds=("any",), group="Navigate", globs=True),
    CommandSpec("find", "Find files and folders by name (a regular expression).", _find_build, _find_run, kinds=("none", "dir"),
                group="Navigate"),
)
