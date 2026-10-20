"""The interface every file backend implements (local disk, WSL, the reMarkable), and the errors they raise.

A command asks `caps` first, so an action a backend cannot do gives one clear message instead of an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...platform import CancelToken

LIST, STAT, READ, WRITE, MKDIR, RENAME, REMOVE, RMDIR, TOUCH, COPY = (
    "list", "stat", "read", "write", "mkdir", "rename", "remove", "rmdir", "touch", "copy")
ALL_CAPS = frozenset({LIST, STAT, READ, WRITE, MKDIR, RENAME, REMOVE, RMDIR, TOUCH, COPY})


class VfsError(Exception):
    """A file operation failed. `str(error)` is the sentence to show; `path` is the virtual path involved."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.message = message
        self.path = path


class NotFound(VfsError):
    pass


class NotADirectory(VfsError):
    pass


class IsADirectory(VfsError):
    pass


class AlreadyExists(VfsError):
    pass


class PermissionDenied(VfsError):
    pass


class DirectoryNotEmpty(VfsError):
    pass


class Unsupported(VfsError):
    """This backend cannot do this (for example, the reMarkable has no `touch`)."""


class BackendFailure(VfsError):
    """The backend itself failed (rmapi not installed, the tablet cloud unreachable...)."""


class PathRefused(VfsError):
    """The path is refused whatever the permissions: a network path, or one that cannot be parsed."""


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None
    is_link: bool = False

    @property
    def hidden(self) -> bool:
        return self.name.startswith(".")


@dataclass(frozen=True, slots=True)
class Stat:
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    is_link: bool = False
    readable: bool = True
    writable: bool = True
    executable: bool = False

    @property
    def is_file(self) -> bool:
        return not self.is_dir


class Backend(Protocol):
    name: str  # "local", "wsl", "remarkable"
    label: str  # for messages
    caps: frozenset[str]

    def list(self, inner: str, cancel: CancelToken | None = None) -> list[Entry]: ...

    def stat(self, inner: str) -> Stat | None:
        """None when the path does not exist."""

    def read(self, inner: str, max_bytes: int | None = None, cancel: CancelToken | None = None) -> bytes: ...

    def write(self, inner: str, data: bytes, cancel: CancelToken | None = None) -> None:
        """Create or replace a file; a crash leaves the old content or the new, never half of it."""

    def mkdir(self, inner: str, parents: bool = False) -> None: ...

    def rename(self, source: str, destination: str) -> None: ...

    def remove(self, inner: str) -> None:
        """Remove a file (or a link, not what it points to)."""

    def rmdir(self, inner: str) -> None:
        """Remove an empty folder."""

    def touch(self, inner: str) -> None: ...

    def join(self, inner: str, name: str) -> str: ...

    def parent(self, inner: str) -> str: ...
