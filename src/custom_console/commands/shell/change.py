"""Change: `cp`, `rm`, `mkdir`, `mv`, `touch`, `rmdir`.

`cp` and `mv` do not overwrite without `-f`. A copy reports every file that failed and a final count (BUG-16), and a
failure does not stop the others. `rm -r` asks first unless `-f`; it never touches a drive root, home, or any folder that
holds the current one.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator

from ...core.vfs.backend import VfsError
from ...core.vfs.ops import CopyReport
from ..base import CommandSpec as Spec
from ..base import Context, Line
from .helpers import DIM, plural, size_text


def _report(ctx: Context, report: CopyReport, verb: str) -> Iterator[Line]:
    for path, why in report.failed:
        ctx.fail(f"{path}: {why}")
    total = report.copied + len(report.failed)
    if total > 1 or report.failed:
        summary = f"{verb} {plural(report.copied, 'file')} ({size_text(report.bytes)}B)"
        if report.failed:
            summary += f"; {len(report.failed)} failed"
        yield Line.of(summary, DIM)


# ---------------------------------------------------------------------- cp
def _cp_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-r", "--recursive", action="store_true", help="copy folders and what they contain")
    p.add_argument("-f", "--force", action="store_true", help="replace files that already exist")
    p.add_argument("paths", nargs="+", help="one or more sources, then the destination")


def _cp_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    if len(a.paths) < 2:
        ctx.fail("give a source and a destination", 2)
        return
    *sources, destination = a.paths
    vfs = ctx.vfs
    try:
        target = vfs.resolve(destination)
        if len(sources) > 1 and not vfs.is_dir(target):
            ctx.fail(f"{destination}: not a folder (several sources need a folder as destination)")
            return
    except VfsError as error:
        ctx.fail(f"{destination}: {error.message}")
        return
    total = CopyReport()
    try:
        for typed in sources:
            try:
                report = vfs.copy(vfs.resolve(typed), target, recursive=a.recursive, force=a.force,
                                  progress=lambda name: ctx.shell.spinner(f"copying {name}"))
            except VfsError as error:
                total.failed.append((typed, error.message))
                continue
            total.merge(report)
    finally:
        ctx.shell.spinner(None)
    yield from _report(ctx, total, "copied")


# ---------------------------------------------------------------------- mv
def _mv_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--force", action="store_true", help="replace files that already exist")
    p.add_argument("paths", nargs="+", help="one or more sources, then the destination")


def _mv_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    if len(a.paths) < 2:
        ctx.fail("give a source and a destination", 2)
        return
    *sources, destination = a.paths
    vfs = ctx.vfs
    try:
        target = vfs.resolve(destination)
        if len(sources) > 1 and not vfs.is_dir(target):
            ctx.fail(f"{destination}: not a folder (several sources need a folder as destination)")
            return
    except VfsError as error:
        ctx.fail(f"{destination}: {error.message}")
        return
    total = CopyReport()
    try:
        for typed in sources:
            try:
                total.merge(vfs.move(vfs.resolve(typed), target, force=a.force,
                                     progress=lambda name: ctx.shell.spinner(f"moving {name}")))
            except VfsError as error:
                total.failed.append((typed, error.message))
    finally:
        ctx.shell.spinner(None)
    yield from _report(ctx, total, "moved")


# ---------------------------------------------------------------------- rm
def _rm_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-r", "--recursive", action="store_true", help="remove folders and what they contain (asks first)")
    p.add_argument("-f", "--force", action="store_true", help="do not ask, and ignore what does not exist")
    p.add_argument("paths", nargs="+", help="what to remove")


def _rm_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    vfs = ctx.vfs
    removed = 0
    for typed in a.paths:
        try:
            target = vfs.resolve(typed)
            info = vfs.stat(target)
            if info is None:
                if not a.force:
                    ctx.fail(f"{typed}: No such file or directory")
                continue
            if info.is_dir and not info.is_link:
                if not a.recursive:
                    ctx.fail(f"{typed}: Is a folder (use -r)")
                    continue
                vfs.refuse_if_protected(target)
                if not a.force and not ctx.shell.confirm(f"Recursively remove {target.virtual}? [y/N] "):
                    ctx.fail(f"{typed}: not removed")
                    continue
                report = vfs.remove_tree(target)
                removed += report.removed
                for path, why in report.failed:
                    ctx.fail(f"{path}: {why}")
            else:
                vfs.remove(target)
                removed += 1
        except VfsError as error:
            ctx.fail(f"{typed}: {error.message}")
    if removed > 1:
        yield Line.of(f"removed {plural(removed, 'item')}", DIM)


# ---------------------------------------------------------------------- mkdir, touch, rmdir
def _mkdir_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--parents", action="store_true", help="make missing parent folders; no error if it exists")
    p.add_argument("paths", nargs="+", help="folders to make")


def _mkdir_run(ctx: Context, a: argparse.Namespace) -> None:
    for typed in a.paths:
        try:
            ctx.vfs.mkdir(ctx.vfs.resolve(typed), parents=a.parents)
        except VfsError as error:
            ctx.fail(f"cannot create '{typed}': {error.message}")


def _touch_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", help="files to create or to mark as just modified")


def _touch_run(ctx: Context, a: argparse.Namespace) -> None:
    for typed in a.paths:
        try:
            ctx.vfs.touch(ctx.vfs.resolve(typed))
        except VfsError as error:
            ctx.fail(f"cannot touch '{typed}': {error.message}")


def _rmdir_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", help="empty folders to remove")


def _rmdir_run(ctx: Context, a: argparse.Namespace) -> None:
    for typed in a.paths:
        try:
            ctx.vfs.rmdir(ctx.vfs.resolve(typed))
        except VfsError as error:
            ctx.fail(f"failed to remove '{typed}': {error.message}")


COMMANDS = (
    Spec("cp", "Copy files and folders (across drives, WSL and the reMarkable). Does not overwrite without -f.", _cp_build, _cp_run,
         kinds=("any",), globs=True, group="Change"),
    Spec("mv", "Move or rename. Across places it copies, checks the size, and only then deletes the source.", _mv_build, _mv_run,
         kinds=("any",), globs=True, group="Change"),
    Spec("rm", "Remove files and, with -r, folders.", _rm_build, _rm_run, kinds=("any",), globs=True, group="Change"),
    Spec("mkdir", "Make folders.", _mkdir_build, _mkdir_run, kinds=("dir",), group="Change"),
    Spec("touch", "Create empty files, or update their time.", _touch_build, _touch_run, kinds=("any",), globs=True, group="Change"),
    Spec("rmdir", "Remove empty folders.", _rmdir_build, _rmdir_run, kinds=("dir",), globs=True, group="Change"),
)
