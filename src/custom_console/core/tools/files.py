"""The file tools: `local_list`, `local_read`, `local_search`, `local_write`, `local_edit`."""

from __future__ import annotations

import re
import time
from typing import Any

from ...platform import CancelToken
from ..diffs import closest_passage, new_file_preview, stats, unified
from ..permissions.engine import READ, WRITE, CallInfo, PathUse
from ..permissions.floor import is_inside, sensitive_reason
from ..undo import bytes_hash
from ..vfs.backend import NotFound, PathRefused, Stat, Unsupported, VfsError
from ..vfs.ops import BinaryFile
from ..vfs.resolver import TABLET, Resolved
from ..vfs.text import (
    SNIFF,
    compile_glob,
    count_lines,
    decode_text,
    line_window,
    looks_binary,
    number_lines,
    split_lines,
)
from .base import BOOL, INT, STR, ToolContext, ToolError, ToolSpec, obj

MAX_WRITE_BYTES = 5_000_000
MAX_LINE_CHARS = 2_000
DEFAULT_READ_LINES = 2_000
LIST_LIMIT = 500
SEARCH_FILE_LIMIT = 2_000_000
SEARCH_BUDGET_SECONDS = 60.0
SKIPPED_FOLDERS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def _resolve(ctx: ToolContext, typed: str) -> Resolved:
    try:
        return ctx.vfs.resolve(typed or ".")
    except (PathRefused, VfsError) as error:
        raise ToolError(str(error)) from None


def _error(error: VfsError, shown: str) -> ToolError:
    return ToolError(f"{error.path or shown}: {error.message}")


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else f"{line[:MAX_LINE_CHARS]}… [+{len(line) - MAX_LINE_CHARS} characters]"


# ---------------------------------------------------------------------- local_list
def _inspect_list(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path") or "."
    return CallInfo("local_list", READ, (PathUse(path),), summary=f"List {path}")


def _human(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return ""


def _run_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args.get("path") or ".")
    show_hidden = bool(args.get("all", False))
    try:
        info = ctx.vfs.stat(target)
        if info is None:
            raise ToolError(f"{target.virtual}: No such file or directory")
        if not info.is_dir:
            return f"{target.virtual}  (file, {_human(info.size)})"
        entries = ctx.vfs.list(target)
    except VfsError as error:
        raise _error(error, target.virtual) from None
    entries = [e for e in entries if show_hidden or not e.hidden]
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    lines = [f"{target.virtual}  ({len(entries)} entries)"]
    for entry in entries[:LIST_LIMIT]:
        mark = "/" if entry.is_dir else ""
        link = " -> link" if entry.is_link else ""
        lines.append(f"{entry.name}{mark}{link}" + (f"  {_human(entry.size)}" if entry.size is not None and not entry.is_dir else ""))
    if len(entries) > LIST_LIMIT:
        lines.append(f"... {len(entries) - LIST_LIMIT} more entries")
    return "\n".join(lines)


LIST = ToolSpec(
    "local_list",
    "List a folder on this computer: names, folders first (a trailing / marks a folder), file sizes. Paths may be virtual: "
    "`/` lists drives and mounts on Windows, `~` is the home folder, `reMarkable:Notes` is the tablet.",
    obj({"path": {**STR, "description": "Folder to list (default: the working folder)"},
         "all": {**BOOL, "description": "Include hidden entries (names starting with a dot)"}}),
    READ, _inspect_list, _run_list, read_only_for_subagents=True)


# ---------------------------------------------------------------------- local_read
def _inspect_read(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path", "")
    return CallInfo("local_read", READ, (PathUse(path),), summary=f"Read {path}")


def read_stat(ctx: ToolContext, target: Resolved) -> Stat:
    try:
        info = ctx.vfs.stat(target)
    except VfsError as error:
        raise _error(error, target.virtual) from None
    if info is None:
        raise ToolError(f"{target.virtual}: No such file or directory")
    return info


def _run_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args["path"])
    info = read_stat(ctx, target)
    if info.is_dir:
        raise ToolError(f"{target.virtual} is a folder: use local_list.")
    if target.mount == TABLET:
        raise ToolError("A reMarkable notebook is not text: convert it with rmdoc2pdf, then use local_pdf_to_markdown.")
    offset = int(args.get("offset", 1) or 1)
    limit = args.get("limit")
    limit = DEFAULT_READ_LINES if limit is None else int(limit)
    try:
        file = ctx.vfs.read_text(target)
    except BinaryFile:
        raise ToolError(f"{target.virtual} is a binary file ({info.size:,} bytes): it cannot be read as text.") from None
    except VfsError as error:
        raise _error(error, target.virtual) from None
    total = count_lines(file.text)
    if total == 0:
        ctx.guard.note_read(target.real(), info, first_line=0, last_line=0, total_lines=0)
        return f"{target.virtual} is empty."
    if offset > total:
        raise ToolError(f"{target.virtual} has only {total} lines; offset {offset} is past the end.")
    window = line_window(file.text, offset, limit)
    whole = window.whole and not file.truncated
    ctx.guard.note_read(target.real(), info, first_line=window.first, last_line=window.last,
                        total_lines=total if not file.truncated else 10**9, whole_file=whole)
    body = number_lines([_clip(line) for line in window.lines], window.first)
    notes = []
    if not whole:
        notes.append(f"[lines {window.first}-{window.last} of {total}" + (f"; read on with offset={window.last + 1}]" if window.more_after else "]"))
    if file.truncated:
        notes.append(f"[the file is {info.size:,} bytes; only the first part was read]")
    return "\n".join([body, *notes])


READ_TOOL = ToolSpec(
    "local_read",
    "Read a text file with line numbers. Long files are read in parts with `offset` (first line, 1-based) and `limit` "
    "(number of lines). To overwrite or edit a file you must first read ALL of it.",
    obj({"path": STR, "offset": {**INT, "description": "First line to read, 1-based (default 1)"},
         "limit": {**INT, "description": f"Number of lines (default {DEFAULT_READ_LINES})"}}, ["path"]),
    READ, _inspect_read, _run_read, read_only_for_subagents=True)


# ---------------------------------------------------------------------- local_search
def _inspect_search(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path") or "."
    what = args.get("pattern") or args.get("glob") or ""
    return CallInfo("local_search", READ, (PathUse(path),), summary=f"Search {path} for {what!r}")


def _relative(start: Resolved, item: Resolved) -> str:
    base = start.virtual.rstrip("/") + "/"
    return item.virtual[len(base):] if item.virtual.startswith(base) else item.virtual


def _run_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    start = _resolve(ctx, args.get("path") or ".")
    pattern = args.get("pattern") or ""
    glob = args.get("glob") or ""
    if not pattern and not glob:
        raise ToolError("Give a `glob` (file names) or a `pattern` (text inside files), or both.")
    ignore_case = bool(args.get("ignore_case", False))
    max_results = max(1, min(int(args.get("max_results", 200) or 200), 2000))
    matcher = None
    if pattern:
        try:
            matcher = re.compile(pattern if args.get("regex") else re.escape(pattern), re.I if ignore_case else 0)
        except re.error as error:
            raise ToolError(f"The pattern is not a valid regular expression: {error}") from None
    if matcher is not None and start.mount == TABLET:
        raise ToolError("Searching inside reMarkable files is not supported; search by name with `glob`.")
    name_filter = compile_glob(glob, ignore_case=ctx.platform.paths.case_insensitive or ignore_case) if glob else None
    info = read_stat(ctx, start)
    files: list[Resolved] = []
    if info.is_dir:
        errors = 0
        skipped_dirs = 0

        def on_error(_folder: Resolved, _error: VfsError) -> None:
            nonlocal errors
            errors += 1

        for walked in ctx.vfs.walk(start, on_error=on_error):
            if walked.entry.is_dir:
                continue
            ctx.cancel.raise_if_cancelled()
            rel = _relative(start, walked.resolved)
            if any(part in SKIPPED_FOLDERS for part in rel.split("/")[:-1]):
                skipped_dirs += 1
                continue
            if name_filter is not None and not (name_filter.fullmatch(rel) or name_filter.fullmatch(rel.rsplit("/", 1)[-1])):
                continue
            files.append(walked.resolved)
    else:
        files.append(start)
        errors = skipped_dirs = 0
    if matcher is None:
        shown = [_relative(start, f) if info.is_dir else f.virtual for f in files[:max_results]]
        more = f"\n... {len(files) - max_results} more" if len(files) > max_results else ""
        return ("\n".join(shown) + more) if shown else "No file matches."
    return _grep_files(ctx, start, files, matcher, max_results, errors, info.is_dir)


def _sensitive(ctx: ToolContext, item: Resolved) -> bool:
    return any(sensitive_reason(form, ctx.floor) for form in (item.virtual, item.real()))


def _grep_files(ctx: ToolContext, start: Resolved, files: list[Resolved], matcher: re.Pattern[str], max_results: int,
                errors: int, is_folder: bool) -> str:
    out: list[str] = []
    skipped_sensitive = skipped_big = skipped_binary = 0
    searched = 0
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    stopped = ""
    for item in files:
        ctx.cancel.raise_if_cancelled()
        if time.monotonic() > deadline:
            stopped = "stopped after a minute"
            break
        if _sensitive(ctx, item):
            skipped_sensitive += 1
            continue
        try:
            stat = ctx.vfs.stat(item)
            if stat is None or stat.is_dir:
                continue
            if stat.size > SEARCH_FILE_LIMIT:
                skipped_big += 1
                continue
            data = ctx.vfs.read_bytes(item, SEARCH_FILE_LIMIT)
        except VfsError:
            errors += 1
            continue
        if looks_binary(data[:SNIFF]):
            skipped_binary += 1
            continue
        searched += 1
        label = _relative(start, item) if is_folder else item.virtual
        for number, line in enumerate(split_lines(decode_text(data)), 1):
            if matcher.search(line[:MAX_LINE_CHARS]):
                out.append(f"{label}:{number}:{_clip(line.strip())[:300]}")
                if len(out) >= max_results:
                    stopped = f"stopped at {max_results} matches"
                    break
        if stopped.startswith("stopped at"):
            break
    notes = [f"{len(out)} matches in {searched} files searched"]
    if stopped:
        notes.append(stopped)
    for count, what in ((skipped_sensitive, "sensitive files skipped"), (skipped_big, "files over 2 MB skipped"),
                        (skipped_binary, "binary files skipped"), (errors, "unreadable entries")):
        if count:
            notes.append(f"{count} {what}")
    return "\n".join([*out, f"[{'; '.join(notes)}]"])


SEARCH = ToolSpec(
    "local_search",
    "Find files by name (`glob`, e.g. `**/*.py` or `*.{md,txt}`) and/or search the text inside files (`pattern`, literal "
    "unless `regex` is true). Results are `path:line:text`; line numbers match local_read.",
    obj({"pattern": {**STR, "description": "Text to look for inside files"},
         "glob": {**STR, "description": "File name pattern: * ? [a-z] ** {a,b}"},
         "path": {**STR, "description": "Where to search (default: the working folder)"},
         "regex": {**BOOL, "description": "Treat pattern as a regular expression"},
         "ignore_case": BOOL, "max_results": {**INT, "description": "At most this many matches (default 200)"}}),
    READ, _inspect_search, _run_search, read_only_for_subagents=True)


# ---------------------------------------------------------------------- local_write / local_edit
def _inspect_write(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path", "")
    return CallInfo("local_write", WRITE, (PathUse(path, "write"),), summary=f"Write {path}", needs_trusted_folder=True,
                    undoable=True)


def _inspect_edit(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path", "")
    return CallInfo("local_edit", WRITE, (PathUse(path, "write"),), summary=f"Edit {path}", needs_trusted_folder=True,
                    undoable=True)


class Existing:
    """The file as it is now: bytes, text, line ending, BOM."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
        self.crlf = crlf > 0 and crlf * 2 >= lf
        try:
            raw.decode("utf-8")
            self.utf8 = True
        except UnicodeDecodeError:
            self.utf8 = False
        self.text = decode_text(raw)

    def encode(self, text: str) -> bytes:
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.crlf:
            body = body.replace("\n", "\r\n")
        data = body.encode("utf-8")
        return b"\xef\xbb\xbf" + data if self.bom else data


def _load_existing(ctx: ToolContext, target: Resolved) -> tuple[Existing | None, Stat | None]:
    try:
        info = ctx.vfs.stat(target)
        if info is None:
            return None, None
        if info.is_dir:
            raise ToolError(f"{target.virtual} is a folder.")
        return Existing(ctx.vfs.read_bytes(target)), info
    except VfsError as error:
        raise _error(error, target.virtual) from None


def _disk_only(target: Resolved) -> None:
    if not target.on_disk:
        raise ToolError(f"{target.virtual}: writing is only supported on disks, not on the reMarkable or the virtual root.")


def _missing_parents(ctx: ToolContext, target: Resolved) -> list[Resolved]:
    missing: list[Resolved] = []
    folder = ctx.vfs.resolver.parent(target)
    while not folder.is_root:
        try:
            if ctx.vfs.exists(folder):
                break
        except VfsError:
            break
        missing.append(folder)
        parent = ctx.vfs.resolver.parent(folder)
        if parent.virtual == folder.virtual:
            break
        folder = parent
    return list(reversed(missing))


def _commit(ctx: ToolContext, target: Resolved, data: bytes, previous: Stat | None, total_lines: int) -> None:
    """Write with an undo record around it, and tell the guard the console now knows the whole file."""
    real = target.real()
    created = _missing_parents(ctx, target) if previous is None else []
    number = None
    if ctx.undo is not None:
        try:
            number = ctx.undo.before(real, [c.real() for c in created])
        except OSError as error:
            raise ToolError(f"{target.virtual}: cannot prepare the undo record ({error}); nothing was changed.") from None
    try:
        for folder in created:
            ctx.vfs.mkdir(folder)
        ctx.vfs.write_bytes(target, data)
    except VfsError as error:
        if number is not None:
            ctx.undo.discard(number)
        raise _error(error, target.virtual) from None
    if number is not None:
        ctx.undo.after(number, data)
    info = ctx.vfs.stat(target)
    if info is not None:
        if previous is None:
            ctx.guard.note_created(real)
        ctx.guard.note_written(real, info, total_lines)


def _check_overwrite(ctx: ToolContext, target: Resolved, existing: Existing | None, info: Stat | None) -> None:
    refusal = ctx.guard.refusal(target.real(), info, target.virtual)
    if refusal:
        raise ToolError(refusal)
    if existing is not None and not existing.utf8:
        raise ToolError(f"{target.virtual} is not valid UTF-8, so it would be corrupted by a text write. Use local_run.")


def _run_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args["path"])
    _disk_only(target)
    content = args["content"]
    if len(content.encode("utf-8", errors="replace")) > MAX_WRITE_BYTES:
        raise ToolError(f"The content is over {MAX_WRITE_BYTES:,} bytes: write it in parts with local_run.")
    existing, info = _load_existing(ctx, target)
    _check_overwrite(ctx, target, existing, info)
    data = existing.encode(content) if existing is not None else content.replace("\r\n", "\n").encode("utf-8")
    if existing is not None and data == existing.raw:
        return f"{target.virtual} already has exactly this content; nothing changed."
    _commit(ctx, target, data, info, count_lines(content))
    if existing is None:
        return f"Created {target.virtual} ({count_lines(content)} lines, {len(data):,} bytes)."
    added, removed = stats(existing.text, decode_text(data))
    return f"Wrote {target.virtual} ({count_lines(content)} lines, {len(data):,} bytes; +{added} -{removed})."


def _preview_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args.get("path", ""))
    content = str(args.get("content", ""))
    try:
        info = ctx.vfs.stat(target)
        if info is not None and not info.is_dir and info.size < 2_000_000:
            return unified(decode_text(ctx.vfs.read_bytes(target)), content, target.virtual)
    except VfsError:
        pass
    return f"New file, {count_lines(content)} lines:\n{new_file_preview(content)}"


WRITE_TOOL = ToolSpec(
    "local_write",
    "Create a file, or replace one completely. An existing file must have been read in full (local_read) first. Missing "
    "folders are created. To change part of a file use local_edit.",
    obj({"path": STR, "content": {**STR, "description": "The whole new content of the file"}}, ["path", "content"]),
    WRITE, _inspect_write, _run_write, preview=_preview_write, needs_trusted_folder=True)


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """The text with `old` replaced by `new`; raises ToolError when `old` is missing or ambiguous."""
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old:
        raise ToolError("old_text is empty.")
    if old == new:
        raise ToolError("old_text and new_text are the same.")
    found = text.count(old)
    if found == 0:
        hint = closest_passage(text, old)
        extra = f" The closest passage starts at line {hint[0]}:\n{hint[1]}" if hint else ""
        raise ToolError("old_text was not found in the file (it must match exactly, spaces and line breaks included)." + extra)
    if found > 1 and not replace_all:
        raise ToolError(f"old_text occurs {found} times: add surrounding lines to make it unique, or set replace_all.")
    return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), found if replace_all else 1


def _run_edit(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args["path"])
    _disk_only(target)
    existing, info = _load_existing(ctx, target)
    if existing is None:
        raise ToolError(f"{target.virtual}: No such file. Use local_write to create it.")
    _check_overwrite(ctx, target, existing, info)
    updated, count = apply_edit(existing.text, args["old_text"], args["new_text"], bool(args.get("replace_all", False)))
    data = existing.encode(updated)
    _commit(ctx, target, data, info, count_lines(updated))
    added, removed = stats(existing.text, updated)
    return f"Edited {target.virtual}: {count} replacement{'s' if count != 1 else ''} (+{added} -{removed})."


def _preview_edit(ctx: ToolContext, args: dict[str, Any]) -> str:
    """The diff the person is asked about. It never searches for a close passage (that is for a failed edit only)."""
    target = _resolve(ctx, args.get("path", ""))
    old, new = str(args.get("old_text", "")), str(args.get("new_text", ""))
    try:
        info = ctx.vfs.stat(target)
        if info is not None and not info.is_dir and info.size < 2_000_000:
            text = decode_text(ctx.vfs.read_bytes(target))
            if old and old.replace("\r\n", "\n") in text:
                updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
                return unified(text, updated, target.virtual)
    except VfsError:
        pass
    return unified(old, new)


EDIT = ToolSpec(
    "local_edit",
    "Replace exact text in a file. `old_text` must match the file exactly and occur once (or set replace_all). The file "
    "must have been read in full (local_read) first.",
    obj({"path": STR, "old_text": STR, "new_text": STR, "replace_all": BOOL}, ["path", "old_text", "new_text"]),
    WRITE, _inspect_edit, _run_edit, preview=_preview_edit, needs_trusted_folder=True)

FILE_TOOLS = (LIST, READ_TOOL, SEARCH, WRITE_TOOL, EDIT)

__all__ = ["FILE_TOOLS", "CancelToken", "NotFound", "Unsupported", "apply_edit", "bytes_hash", "is_inside"]
