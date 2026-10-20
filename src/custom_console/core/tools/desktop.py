"""The desktop tools: open a file, start an app, read and write the clipboard. All of them ask; the clipboard asks every
time. Opening is an allowlist of passive file types (SEC-5): anything that could run is refused, whatever its name."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from ...platform import DesktopError
from ..diffs import cap_rows
from ..permissions.engine import CLIPBOARD, LAUNCH, OPEN, CallInfo, PathUse
from .base import STR, ToolContext, ToolError, ToolSpec, cut_middle, obj
from .files import _resolve

# Files that only display. No macro-enabled formats, no scripts, no shortcuts, no installers, no archives.
PASSIVE_TYPES = frozenset({
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".rtf", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".ico",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".mov", ".webm", ".avi",
})
MAX_CLIPBOARD_WRITE = 1_000_000


def extension(name: str) -> str:
    """The extension the system would use: trailing dots and spaces are ignored by Windows, so `x.cmd.` is `.cmd`."""
    base = posixpath.basename(name.replace("\\", "/")).rstrip(". ")
    return posixpath.splitext(base)[1].lower()


def is_passive(name: str) -> bool:
    return extension(name) in PASSIVE_TYPES


# ---------------------------------------------------------------------- local_open_path
def _inspect_open(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_open_path", OPEN, (PathUse(args.get("path", "")),), summary=f"Open {args.get('path', '')}")


def _run_open(ctx: ToolContext, args: dict[str, Any]) -> str:
    target = _resolve(ctx, args["path"])
    if not target.on_disk:
        raise ToolError("Only files and folders on a disk can be opened (copy a reMarkable file out first).")
    info = ctx.vfs.stat(target)
    if info is None:
        raise ToolError(f"{target.virtual}: No such file or directory")
    if not info.is_dir and not is_passive(target.real()):
        raise ToolError(f"{target.virtual}: files of type {extension(target.real()) or '(none)'} are not opened by the agent "
                        "(only documents, pictures and media are). Ask the person to open it.")
    try:
        ctx.platform.desktop.open_path(Path(target.real()))
    except DesktopError as error:
        raise ToolError(str(error)) from None
    return f"Opened {target.virtual}."


OPEN_TOOL = ToolSpec(
    "local_open_path",
    "Open a folder, or a document, picture or media file, with its default application on the person's computer. "
    "Programs, scripts and other files that could run are refused.",
    obj({"path": STR}, ["path"]), OPEN, _inspect_open, _run_open)


# ---------------------------------------------------------------------- local_launch_app
def _inspect_launch(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    name = str(args.get("name", "")).strip()
    if not name:
        raise ToolError("Give the name of the application.")
    match = ctx.platform.desktop.find_app(name, 3)
    if match is None:
        raise ToolError(f"No application called {name!r} was found (searched the PATH, desktop entries and install folders).")
    ctx.extras["app"] = match
    return CallInfo("local_launch_app", LAUNCH, command=name, summary=f"Start {match.name} ({match.target})")


def _run_launch(ctx: ToolContext, args: dict[str, Any]) -> str:
    name = str(args["name"]).strip()
    match = ctx.extras.get("app") or ctx.platform.desktop.find_app(name, 3)
    if match is None:
        raise ToolError(f"No application called {name!r} was found.")
    try:
        ctx.platform.desktop.start_app(match)
    except DesktopError as error:
        raise ToolError(str(error)) from None
    return f"Started {match.name} ({match.target})."


LAUNCH_TOOL = ToolSpec(
    "local_launch_app",
    "Start an installed application by name (it is looked up on the PATH, in desktop entries and install folders).",
    obj({"name": STR}, ["name"]), LAUNCH, _inspect_launch, _run_launch)


# ---------------------------------------------------------------------- clipboard
def _inspect_clip_read(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_clipboard_read", CLIPBOARD, summary="Read the clipboard", remember=False)


def _run_clip_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        text = ctx.platform.desktop.clipboard_read()
    except DesktopError as error:
        raise ToolError(str(error)) from None
    return cut_middle(text, ctx.result_limit) if text else "The clipboard is empty."


CLIPBOARD_READ = ToolSpec(
    "local_clipboard_read", "Read the text on the clipboard. The person is asked every time.",
    obj({}), CLIPBOARD, _inspect_clip_read, _run_clip_read)


def _inspect_clip_write(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_clipboard_write", CLIPBOARD, summary=f"Replace the clipboard ({len(str(args.get('text', '')))} characters)",
                    remember=False)


def _preview_clip_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    return cap_rows(str(args.get("text", "")).split("\n"), 20)


def _run_clip_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    text = str(args["text"])
    if len(text) > MAX_CLIPBOARD_WRITE:
        raise ToolError(f"The text is over {MAX_CLIPBOARD_WRITE:,} characters.")
    try:
        ctx.platform.desktop.clipboard_write(text)
    except DesktopError as error:
        raise ToolError(str(error)) from None
    return f"The clipboard now holds {len(text):,} characters."


CLIPBOARD_WRITE = ToolSpec(
    "local_clipboard_write", "Replace the clipboard with text. The person is asked every time.",
    obj({"text": STR}, ["text"]), CLIPBOARD, _inspect_clip_write, _run_clip_write, preview=_preview_clip_write)

DESKTOP_TOOLS = (OPEN_TOOL, LAUNCH_TOOL, CLIPBOARD_READ, CLIPBOARD_WRITE)
