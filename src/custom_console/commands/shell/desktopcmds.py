"""Desktop: `launch`, `open`, `clip`."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator
from pathlib import Path

from ...core.vfs.backend import VfsError
from ...core.vfs.resolver import TABLET, Resolved
from ...core.vfs.rmdoc import MissingDependency, RmdocError, convert
from ...platform import DesktopError
from ..base import CommandError, Context, Line
from ..base import CommandSpec as Spec
from .helpers import DIM


# ---------------------------------------------------------------------- launch
def _launch_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("app", help="the application's name (or its path, with -f)")
    p.add_argument("-f", "--file", action="store_true", help="the argument is a path to start as it is")
    p.add_argument("-sl", "--search-level", "--search_level", dest="level", type=int, default=4,
                   help="how far to look, 1 to 4 (default 4; -1 looks everywhere). 1: the PATH, 2: the system's list of apps, "
                        "3: install folders, 4: a slow walk of them")


def _launch_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    desktop = ctx.shell.platform.desktop
    try:
        if a.file:
            target = ctx.vfs.resolve(a.app)
            if not target.on_disk or not ctx.vfs.exists(target):
                ctx.fail(f"{a.app}: No such file")
                return
            desktop.start_file(Path(target.real()))
            yield Line.of(f"Started {target.virtual}")
            return
        level = 4 if a.level < 0 else max(1, min(a.level, 4))
        cache = ctx.shell.apps
        remembered = cache.get(a.app)
        if remembered is not None:
            desktop.start_file(Path(remembered))
            yield Line.of(f"Started {a.app} ({remembered})")
            return
        ctx.shell.spinner(f"looking for {a.app}")
        try:
            match = desktop.find_app(a.app, level)
        finally:
            ctx.shell.spinner(None)
        if match is None:
            ctx.fail(f"{a.app}: not found (searched {'the first ' + str(level) + ' of 4 stages' if level < 4 else 'everywhere'}"
                     f"{'; try a higher -sl' if level < 4 else ''})")
            return
        desktop.start_app(match)
        if not match.target.endswith((".desktop", ".app")):
            cache.remember(a.app, match.target)
        yield Line.of(f"Started {match.name} ({match.target})")
    except (DesktopError, VfsError) as error:
        ctx.fail(error.message if isinstance(error, VfsError) else str(error))


# ---------------------------------------------------------------------- open
def _open_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", help="a file or folder to open with its default application")


def _open_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    desktop = ctx.shell.platform.desktop
    try:
        target = ctx.vfs.resolve(a.path)
        info = ctx.vfs.stat(target)
        if info is None:
            ctx.fail(f"{a.path}: No such file or directory")
            return
        if target.mount == TABLET and not info.is_dir:
            local = _download(ctx, target)  # a reMarkable file is downloaded to a temporary file first
        elif target.on_disk:
            local = Path(target.real())
        else:
            ctx.fail(f"{a.path}: nothing to open here")
            return
        desktop.open_path(local)
        yield Line.of(f"Opened {local}" if target.mount == TABLET else f"Opened {target.virtual}", DIM)
    except (DesktopError, VfsError, RmdocError) as error:
        ctx.fail(error.message if isinstance(error, VfsError) else str(error))


def _download(ctx: Context, target: Resolved) -> Path:
    folder = ctx.shell.layout.tmp
    folder.mkdir(parents=True, exist_ok=True)
    ctx.shell.spinner(f"downloading {target.virtual}")
    try:
        data = ctx.vfs.read_bytes(target)
    finally:
        ctx.shell.spinner(None)
    name = target.vpath.parts[-1] if target.vpath.parts else "document"
    stem = f"{name}-{uuid.uuid4().hex[:6]}"
    try:
        result = convert(data, cancel=ctx.shell.cancel)
        path = folder / f"{stem}.pdf"
        path.write_bytes(result.pdf)
    except (MissingDependency, RmdocError):
        path = folder / f"{stem}.rmdoc"  # no converter: the archive itself
        path.write_bytes(data)
    return path


# ---------------------------------------------------------------------- clip
def _clip_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--paste", action="store_true", help="print the clipboard instead of setting it")
    p.add_argument("-f", "--file", metavar="PATH", help="copy the text of a file")
    p.add_argument("text", nargs="*", help="text to copy (default: the piped input)")


def _clip_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    desktop = ctx.shell.platform.desktop
    try:
        if a.paste:
            from ...core.vfs.text import split_lines
            for text in split_lines(desktop.clipboard_read()):
                yield Line.of(text)
            return
        if a.file:
            from .read import read_file
            _, text = read_file(ctx, a.file)
        elif a.text:
            text = " ".join(a.text)
        elif ctx.has_stdin:
            text = "\n".join(line.plain for line in ctx.stdin_lines())
        else:
            ctx.fail("nothing to copy: give text, -f FILE, or pipe something in", 2)
            return
        desktop.clipboard_write(text)
        yield Line.of(f"copied {len(text):,} characters", DIM)
    except DesktopError as error:
        ctx.fail(str(error))
    except (VfsError, CommandError) as error:
        ctx.fail(error.message if isinstance(error, VfsError) else str(error))


COMMANDS = (
    Spec("launch", "Find an installed application by name and start it.", _launch_build, _launch_run, kinds=("app",), group="Desktop"),
    Spec("open", "Open a file or folder with its default application.", _open_build, _open_run, kinds=("any",), group="Desktop"),
    Spec("clip", "Copy text (piped in, from a file, or typed) to the clipboard; clip -p prints it.", _clip_build, _clip_run,
         kinds=("none",), group="Desktop"),
)
