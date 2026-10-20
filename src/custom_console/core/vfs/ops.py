"""File operations over the virtual tree: what the shell commands and the agent tools both call.

Every operation takes a `Resolved` path, checks the backend's capability flags first (one clear message instead of an
exception), and passes the cancel token down. Nothing here prints; results are data.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from ...platform import CancelToken
from .backend import (
    LIST,
    MKDIR,
    READ,
    REMOVE,
    RENAME,
    RMDIR,
    STAT,
    TOUCH,
    WRITE,
    AlreadyExists,
    Entry,
    IsADirectory,
    NotADirectory,
    NotFound,
    PathRefused,
    Stat,
    Unsupported,
    VfsError,
)
from .resolver import DRIVE, POSIX, TABLET, WSL, Resolved, Resolver
from .text import decode_text, looks_binary

DEFAULT_READ_LIMIT = 10_000_000


class BinaryFile(VfsError):
    pass


@dataclass(frozen=True, slots=True)
class TextFile:
    text: str
    size: int  # bytes on disk
    truncated: bool  # more bytes exist than were read


@dataclass(slots=True)
class CopyReport:
    copied: int = 0
    bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, why): the run goes on after a failure

    def merge(self, other: CopyReport) -> None:
        self.copied += other.copied
        self.bytes += other.bytes
        self.failed += other.failed

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass(slots=True)
class RemoveReport:
    removed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass(frozen=True, slots=True)
class Walked:
    resolved: Resolved
    entry: Entry
    depth: int  # 1 for the direct children of the start


class Vfs:
    """The virtual file system as one object: a resolver, a current folder of its own, and the operations."""

    def __init__(self, resolver: Resolver, cwd: str | None = None, cancel: CancelToken | None = None):
        self.resolver = resolver
        self.cwd = cwd if cwd is not None else resolver.start_directory()
        self.cancel = cancel or CancelToken()
        self._home = resolver.resolve("~", "/").virtual
        # every home this system could mean (the resolver's, and the platform's): none of them, and nothing that holds
        # one, may be removed or moved
        platform_home = resolver.syntax.format(resolver.syntax.from_os_path(str(resolver.platform.paths.home())))
        self._homes = tuple(dict.fromkeys((self._home, platform_home)))
        self._real_homes = tuple(dict.fromkeys(
            os.path.normcase(os.path.realpath(str(h))) for h in (resolver.home_path, resolver.platform.paths.home())))
        self.generation = 0  # grows with every change made through this object: caches of listings go stale when it moves

    def fork(self, cancel: CancelToken | None = None) -> Vfs:
        """A copy that starts in the same folder but has its own cancel token (for work in a background thread)."""
        return Vfs(self.resolver, self.cwd, cancel or CancelToken())

    def _changed(self) -> None:
        self.generation += 1

    # ------------------------------------------------------------------ paths
    def resolve(self, typed: str) -> Resolved:
        return self.resolver.resolve(typed, self.cwd)

    def child(self, parent: Resolved, name: str) -> Resolved:
        return self.resolver.child(parent, name)

    def chdir(self, typed: str) -> Resolved:
        """Change the current folder (no `os.chdir`: each Vfs has its own)."""
        target = self.resolve(typed)
        info = self.stat(target)
        if info is None:
            raise NotFound("No such file or directory", target.virtual)
        if not info.is_dir:
            raise NotADirectory("Not a directory", target.virtual)
        self.cwd = target.virtual
        return target

    def here(self) -> Resolved:
        return self.resolve(".")

    def _need(self, resolved: Resolved, cap: str, what: str) -> None:
        backend = resolved.backend
        if backend is None:
            raise Unsupported(f"The virtual root cannot be {what}.", resolved.virtual)
        if cap not in backend.caps:
            raise Unsupported(f"{backend.label.capitalize()} cannot {what}.", resolved.virtual)

    def supports(self, resolved: Resolved, cap: str) -> bool:
        return resolved.backend is not None and cap in resolved.backend.caps

    # ------------------------------------------------------------------ looking
    def stat(self, resolved: Resolved) -> Stat | None:
        if resolved.is_root:
            return Stat(is_dir=True)
        self._need(resolved, STAT, "be inspected")
        assert resolved.backend is not None
        try:
            return resolved.backend.stat(resolved.inner)
        except VfsError as error:
            error.path = error.path or resolved.virtual
            raise

    def exists(self, resolved: Resolved) -> bool:
        return self.stat(resolved) is not None

    def is_dir(self, resolved: Resolved) -> bool:
        info = self.stat(resolved)
        return info is not None and info.is_dir

    def list(self, resolved: Resolved) -> list[Entry]:
        if resolved.is_root:
            return self.resolver.mounts()
        self._need(resolved, LIST, "list folders")
        assert resolved.backend is not None
        try:
            return resolved.backend.list(resolved.inner, self.cancel)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def walk(self, start: Resolved, *, max_depth: int | None = None, on_error: Callable[[Resolved, VfsError], None] | None = None,
             include_hidden: bool = True) -> Iterator[Walked]:
        """Everything under `start`, folder by folder (files first, then folders, each sorted); links are not followed.
        An unreadable folder is reported to `on_error` and the walk goes on."""
        def visit(folder: Resolved, depth: int) -> Iterator[Walked]:
            self.cancel.raise_if_cancelled()
            try:
                entries = self.list(folder)
            except VfsError as error:
                if on_error is not None:
                    on_error(folder, error)
                return
            entries = [e for e in entries if include_hidden or not e.hidden]
            files = sorted((e for e in entries if not e.is_dir), key=lambda e: e.name.lower())
            folders = sorted((e for e in entries if e.is_dir), key=lambda e: e.name.lower())
            for entry in files:
                yield Walked(self.child(folder, entry.name), entry, depth)
            for entry in folders:
                child = self.child(folder, entry.name)
                yield Walked(child, entry, depth)
                if (max_depth is None or depth < max_depth) and not entry.is_link:
                    yield from visit(child, depth + 1)

        yield from visit(start, 1)

    # ------------------------------------------------------------------ reading
    def read_bytes(self, resolved: Resolved, max_bytes: int | None = None) -> bytes:
        self._need(resolved, READ, "be read")
        assert resolved.backend is not None
        info = self.stat(resolved)
        if info is None:
            raise NotFound("No such file or directory", resolved.virtual)
        if info.is_dir:
            raise IsADirectory("Is a directory", resolved.virtual)
        try:
            return resolved.backend.read(resolved.inner, max_bytes, self.cancel)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def read_text(self, resolved: Resolved, max_bytes: int = DEFAULT_READ_LIMIT) -> TextFile:
        """The text of a file: UTF-8, line ends as `\\n`; a binary file is refused."""
        data = self.read_bytes(resolved, max_bytes + 1)
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        if looks_binary(data):
            raise BinaryFile("Binary file", resolved.virtual)
        info = self.stat(resolved)
        return TextFile(decode_text(data), info.size if info else len(data), truncated)

    # ------------------------------------------------------------------ changing
    def write_bytes(self, resolved: Resolved, data: bytes) -> None:
        self._need(resolved, WRITE, "be written")
        assert resolved.backend is not None
        self._changed()
        try:
            resolved.backend.write(resolved.inner, data, self.cancel)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def mkdir(self, resolved: Resolved, parents: bool = False) -> None:
        self._need(resolved, MKDIR, "create folders")
        assert resolved.backend is not None
        if not parents and self.exists(resolved):
            raise AlreadyExists("Already exists", resolved.virtual)
        self._changed()
        try:
            resolved.backend.mkdir(resolved.inner, parents)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def touch(self, resolved: Resolved) -> None:
        self._need(resolved, TOUCH, "touch files")
        assert resolved.backend is not None
        self._changed()
        try:
            resolved.backend.touch(resolved.inner)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def rmdir(self, resolved: Resolved) -> None:
        self._need(resolved, RMDIR, "remove folders")
        assert resolved.backend is not None
        self.refuse_if_protected(resolved)
        info = self.stat(resolved)
        if info is None:
            raise NotFound("No such file or directory", resolved.virtual)
        if not info.is_dir:
            raise NotADirectory("Not a directory", resolved.virtual)
        self._changed()
        try:
            resolved.backend.rmdir(resolved.inner)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def remove(self, resolved: Resolved) -> None:
        """Remove a file or a link (never what a link points to)."""
        self._need(resolved, REMOVE, "remove files")
        assert resolved.backend is not None
        self.refuse_if_protected(resolved)
        info = self.stat(resolved)
        if info is None:
            raise NotFound("No such file or directory", resolved.virtual)
        if info.is_dir and not info.is_link:
            raise IsADirectory("Is a directory (use -r)", resolved.virtual)
        self._changed()
        try:
            resolved.backend.remove(resolved.inner)
        except VfsError as error:
            error.path = resolved.virtual
            raise

    def remove_tree(self, resolved: Resolved) -> RemoveReport:
        """Remove a folder and everything in it. A failure is recorded and the rest goes on."""
        self._changed()
        self._need(resolved, REMOVE, "remove files")
        self.refuse_if_protected(resolved)
        report = RemoveReport()
        info = self.stat(resolved)
        if info is None:
            raise NotFound("No such file or directory", resolved.virtual)
        if not info.is_dir or info.is_link:
            self.remove(resolved)
            report.removed = 1
            return report

        def clear(folder: Resolved) -> None:
            try:
                entries = self.list(folder)
            except VfsError as error:
                report.failed.append((folder.virtual, error.message))
                return
            for entry in entries:
                self.cancel.raise_if_cancelled()
                child = self.child(folder, entry.name)
                try:
                    if entry.is_dir and not entry.is_link:
                        clear(child)
                        self.rmdir_unchecked(child)
                    else:
                        assert child.backend is not None
                        child.backend.remove(child.inner)
                    report.removed += 1
                except VfsError as error:
                    report.failed.append((child.virtual, error.message))

        clear(resolved)
        if report.ok:
            try:
                self.rmdir_unchecked(resolved)
                report.removed += 1
            except VfsError as error:
                report.failed.append((resolved.virtual, error.message))
        return report

    def rmdir_unchecked(self, resolved: Resolved) -> None:
        assert resolved.backend is not None
        self._changed()
        resolved.backend.rmdir(resolved.inner)

    # ------------------------------------------------------------------ protected places
    def protected_reason(self, resolved: Resolved) -> str | None:
        """Why this place may never be removed or moved: the root, a drive or mount root, home, or any folder holding the
        current folder."""
        if resolved.is_root or (resolved.vpath.is_mount_root and resolved.mount in (DRIVE, WSL, TABLET, POSIX)):
            return "this is the root of a drive or mount"
        if resolved.on_disk and not os.path.islink(resolved.inner):  # a link itself may be removed; what it points to may not
            real = os.path.normcase(os.path.realpath(resolved.inner))
            for home in self._real_homes:
                if real == home:
                    return "this is your home folder"
                if home.startswith(real.rstrip(os.sep) + os.sep):
                    return "your home folder is inside it"
        base = resolved.virtual.rstrip("/") + "/"
        for home in self._homes:
            inside = home.rstrip("/") + "/"
            if inside == base:
                return "this is your home folder"
            if inside.startswith(base):
                return "your home folder is inside it"
        here = self.cwd.rstrip("/") + "/"
        if here.startswith(resolved.virtual.rstrip("/") + "/"):
            return "the current folder is inside it"
        return None

    def refuse_if_protected(self, resolved: Resolved) -> None:
        reason = self.protected_reason(resolved)
        if reason:
            raise PathRefused(f"Refusing: {reason}", resolved.virtual)

    # ------------------------------------------------------------------ copy and move
    def _target_for(self, source: Resolved, destination: Resolved, source_is_dir: bool = False) -> Resolved:
        """`cp a dir/` puts `a` inside `dir`; otherwise the destination is the new name. A document taken off the tablet
        gets the name of its archive (`Plan` becomes `Plan.rmdoc`)."""
        info = self.stat(destination) if not destination.is_root else Stat(is_dir=True)
        if info is not None and info.is_dir:
            name = posixpath.basename(source.virtual.rstrip("/")) or source.vpath.name
            exporter = getattr(source.backend, "export_name", None)
            if exporter is not None and not source_is_dir and source.backend is not destination.backend:
                name = exporter(source.inner)
            return self.child(destination, name)
        return destination

    def _inside(self, folder: Resolved, candidate: Resolved) -> bool:
        base = folder.virtual.rstrip("/") + "/"
        return (candidate.virtual.rstrip("/") + "/").startswith(base)

    def copy(self, source: Resolved, destination: Resolved, *, recursive: bool = False, force: bool = False,
             progress: Callable[[str], None] | None = None) -> CopyReport:
        """Copy a file, or a folder with `recursive`. Does not overwrite without `force`. The size of every copy is
        checked. A failed file is recorded and the copy goes on; the report holds the final count."""
        self._need(source, READ, "be read")
        self._need(destination, WRITE, "be written")
        info = self.stat(source)
        if info is None:
            raise NotFound("No such file or directory", source.virtual)
        target = self._target_for(source, destination, info.is_dir)
        report = CopyReport()
        if info.is_dir:
            if not recursive:
                raise IsADirectory("Is a folder (use -r)", source.virtual)
            if self._inside(source, target):
                raise PathRefused("Cannot copy a folder into itself", source.virtual)
            self._copy_tree(source, target, force, report, progress)
        else:
            self._copy_file(source, target, force, report, progress)
        return report

    def _copy_file(self, source: Resolved, target: Resolved, force: bool, report: CopyReport,
                   progress: Callable[[str], None] | None) -> None:
        try:
            self.cancel.raise_if_cancelled()
            existing = self.stat(target)
            if existing is not None and existing.is_dir:
                raise IsADirectory("Is a folder", target.virtual)
            if existing is not None and not force:
                raise AlreadyExists("Already exists (use -f to overwrite)", target.virtual)
            data = self.read_bytes(source)
            self.write_bytes(target, data)
            if self.supports(target, STAT):
                made = self.stat(target)
                if made is not None and made.size != len(data):
                    raise VfsError(f"The copy has {made.size} bytes, expected {len(data)}", target.virtual)
            report.copied += 1
            report.bytes += len(data)
            if progress:
                progress(source.virtual)
        except VfsError as error:
            report.failed.append((source.virtual, error.message))

    def _copy_tree(self, source: Resolved, target: Resolved, force: bool, report: CopyReport,
                   progress: Callable[[str], None] | None) -> None:
        try:
            if not self.exists(target):
                self.mkdir(target)
            entries = self.list(source)
        except VfsError as error:
            report.failed.append((source.virtual, error.message))
            return
        for entry in sorted(entries, key=lambda e: (e.is_dir, e.name.lower())):
            self.cancel.raise_if_cancelled()
            src, dst = self.child(source, entry.name), self.child(target, entry.name)
            if entry.is_dir and not entry.is_link:
                self._copy_tree(src, dst, force, report, progress)
            else:
                self._copy_file(src, dst, force, report, progress)

    def move(self, source: Resolved, destination: Resolved, *, force: bool = False,
             progress: Callable[[str], None] | None = None) -> CopyReport:
        """Rename within a backend; across backends copy, check the sizes, and only then delete the source."""
        self.refuse_if_protected(source)
        info = self.stat(source)
        if info is None:
            raise NotFound("No such file or directory", source.virtual)
        target = self._target_for(source, destination, info.is_dir)
        if self._inside(source, target):
            raise PathRefused("Cannot move a folder into itself", source.virtual)
        existing = self.stat(target)
        if existing is not None and not force:
            raise AlreadyExists("Already exists (use -f to overwrite)", target.virtual)
        if existing is not None and existing.is_dir and not info.is_dir:
            raise IsADirectory("Is a folder", target.virtual)
        report = CopyReport()
        self._changed()
        if source.backend is target.backend and self.supports(source, RENAME):
            if existing is not None and not existing.is_dir:
                self.remove(target)
            try:
                assert source.backend is not None
                source.backend.rename(source.inner, target.inner)
                report.copied = 1
                return report
            except Unsupported:
                pass  # across devices: fall through to copy and delete
        copied = self.copy(source, target, recursive=True, force=force, progress=progress)
        if not copied.ok:
            return copied  # the source stays
        if info.is_dir:
            removed = self.remove_tree(source)
            copied.failed += removed.failed
        else:
            self.remove(source)
        return copied

    @property
    def home(self) -> str:
        return self._home
