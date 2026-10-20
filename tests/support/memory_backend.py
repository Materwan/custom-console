"""An in-memory backend for tests: stands in for the tablet (a second backend with fewer capabilities)."""

from __future__ import annotations

import posixpath

from custom_console.core.vfs.backend import (
    AlreadyExists,
    DirectoryNotEmpty,
    Entry,
    NotADirectory,
    NotFound,
    Stat,
    VfsError,
)
from custom_console.platform import CancelToken


class MemoryBackend:
    name = "remarkable"
    label = "the memory tablet"

    def __init__(self, caps: frozenset[str] | None = None):
        self.caps = caps or frozenset({"list", "stat", "read", "write", "mkdir", "rename", "remove", "rmdir"})
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.fail_on: set[str] = set()  # paths whose write fails
        self.short_write: set[str] = set()  # paths whose write silently stores fewer bytes

    def list(self, inner: str, cancel: CancelToken | None = None) -> list[Entry]:
        inner = posixpath.normpath(inner)
        if inner not in self.dirs:
            raise NotFound("No such file or directory", inner) if inner not in self.files else NotADirectory("Not a directory", inner)
        out = [Entry(posixpath.basename(p), True) for p in self.dirs if p != "/" and posixpath.dirname(p) == inner]
        out += [Entry(posixpath.basename(p), False, len(d)) for p, d in self.files.items() if posixpath.dirname(p) == inner]
        return out

    def stat(self, inner: str) -> Stat | None:
        inner = posixpath.normpath(inner)
        if inner in self.dirs:
            return Stat(True)
        if inner in self.files:
            return Stat(False, len(self.files[inner]))
        return None

    def read(self, inner: str, max_bytes: int | None = None, cancel: CancelToken | None = None) -> bytes:
        data = self.files.get(posixpath.normpath(inner))
        if data is None:
            raise NotFound("No such file or directory", inner)
        return data if max_bytes is None else data[:max_bytes]

    def write(self, inner: str, data: bytes, cancel: CancelToken | None = None) -> None:
        inner = posixpath.normpath(inner)
        if inner in self.fail_on:
            raise VfsError("the tablet cloud refused the upload", inner)
        if posixpath.dirname(inner) not in self.dirs:
            raise NotFound("No such file or directory", inner)
        self.files[inner] = data[:-1] if inner in self.short_write else data

    def mkdir(self, inner: str, parents: bool = False) -> None:
        inner = posixpath.normpath(inner)
        if inner in self.dirs or inner in self.files:
            raise AlreadyExists("Already exists", inner)
        if posixpath.dirname(inner) not in self.dirs and not parents:
            raise NotFound("No such file or directory", inner)
        self.dirs.add(inner)

    def rename(self, source: str, destination: str) -> None:
        source, destination = posixpath.normpath(source), posixpath.normpath(destination)
        if source in self.files:
            self.files[destination] = self.files.pop(source)
        elif source in self.dirs:
            self.dirs.discard(source)
            self.dirs.add(destination)
        else:
            raise NotFound("No such file or directory", source)

    def remove(self, inner: str) -> None:
        if self.files.pop(posixpath.normpath(inner), None) is None:
            raise NotFound("No such file or directory", inner)

    def rmdir(self, inner: str) -> None:
        inner = posixpath.normpath(inner)
        if any(posixpath.dirname(p) == inner for p in (*self.files, *self.dirs) if p != inner):
            raise DirectoryNotEmpty("Directory not empty", inner)
        self.dirs.discard(inner)

    def touch(self, inner: str) -> None:
        raise VfsError("touch is not supported", inner)

    def export_name(self, inner: str) -> str:
        return posixpath.basename(posixpath.normpath(inner)) + ".rmdoc"

    def join(self, inner: str, name: str) -> str:
        return posixpath.join(inner, name)

    def parent(self, inner: str) -> str:
        return posixpath.dirname(inner) or "/"
