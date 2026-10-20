"""The local disk (and, on Windows, any drive)."""

from __future__ import annotations

import contextlib
import errno
import os
import stat as stat_module
import sys
import tempfile
from pathlib import PurePath

from ...platform import CancelToken
from .backend import (
    ALL_CAPS,
    AlreadyExists,
    DirectoryNotEmpty,
    Entry,
    IsADirectory,
    NotADirectory,
    NotFound,
    PermissionDenied,
    Stat,
    Unsupported,
    VfsError,
)

WINDOWS = sys.platform == "win32"


def translate(error: OSError, path: str) -> VfsError:
    """An OSError as the matching VfsError, with a plain sentence."""
    code = error.errno
    if isinstance(error, FileNotFoundError) or code == errno.ENOENT:
        return NotFound("No such file or directory", path)
    if isinstance(error, NotADirectoryError) or code == errno.ENOTDIR:
        return NotADirectory("Not a directory", path)
    if isinstance(error, IsADirectoryError) or code == errno.EISDIR:
        return IsADirectory("Is a directory", path)
    if isinstance(error, FileExistsError) or code == errno.EEXIST:
        return AlreadyExists("Already exists", path)
    if isinstance(error, PermissionError) or code in (errno.EACCES, errno.EPERM):
        return PermissionDenied("Permission denied", path)
    if code == errno.ENOTEMPTY:
        return DirectoryNotEmpty("Directory not empty", path)
    if code == errno.EXDEV:
        return Unsupported("Cannot move across devices in one step", path)
    return VfsError(error.strerror or str(error), path)


class LocalBackend:
    name = "local"
    label = "the local disk"
    caps = ALL_CAPS

    def list(self, inner: str, cancel: CancelToken | None = None) -> list[Entry]:
        entries: list[Entry] = []
        try:
            with os.scandir(inner) as scan:
                for item in scan:
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    try:
                        is_dir = item.is_dir()
                    except OSError:
                        is_dir = False
                    size: int | None = None
                    mtime: float | None = None
                    try:
                        info = item.stat()
                        size, mtime = info.st_size, info.st_mtime
                    except OSError:
                        pass
                    entries.append(Entry(item.name, is_dir, size if not is_dir else None, mtime, item.is_symlink()))
        except OSError as error:
            raise translate(error, inner) from None
        return entries

    def stat(self, inner: str) -> Stat | None:
        try:
            info = os.stat(inner)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise translate(error, inner) from None
        is_link = False
        with contextlib.suppress(OSError):
            is_link = stat_module.S_ISLNK(os.lstat(inner).st_mode)
        return Stat(stat_module.S_ISDIR(info.st_mode), info.st_size, info.st_mtime, is_link,
                    os.access(inner, os.R_OK), os.access(inner, os.W_OK), os.access(inner, os.X_OK))

    def read(self, inner: str, max_bytes: int | None = None, cancel: CancelToken | None = None) -> bytes:
        try:
            with open(inner, "rb") as handle:
                if max_bytes is None:
                    chunks = []
                    while chunk := handle.read(1 << 20):
                        if cancel is not None:
                            cancel.raise_if_cancelled()
                        chunks.append(chunk)
                    return b"".join(chunks)
                return handle.read(max_bytes)
        except OSError as error:
            raise translate(error, inner) from None

    def write(self, inner: str, data: bytes, cancel: CancelToken | None = None) -> None:
        target = os.path.realpath(inner) if os.path.islink(inner) else inner  # write through a link, keep the link
        folder = os.path.dirname(os.path.abspath(target))
        mode: int | None = None
        with contextlib.suppress(OSError):
            mode = stat_module.S_IMODE(os.stat(target).st_mode)
        try:
            descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".console-")
        except OSError as error:
            raise translate(error, inner) from None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if cancel is not None:
                cancel.raise_if_cancelled()
            if mode is not None:
                with contextlib.suppress(OSError):
                    os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException as error:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            if isinstance(error, OSError):
                raise translate(error, inner) from None
            raise

    def mkdir(self, inner: str, parents: bool = False) -> None:
        try:
            if parents:
                os.makedirs(inner, exist_ok=True)
            else:
                os.mkdir(inner)
        except OSError as error:
            raise translate(error, inner) from None

    def rename(self, source: str, destination: str) -> None:
        try:
            os.rename(source, destination)
        except OSError as error:
            raise translate(error, source) from None

    def remove(self, inner: str) -> None:
        try:
            os.unlink(inner)
        except PermissionError:
            if not WINDOWS:
                raise PermissionDenied("Permission denied", inner) from None
            try:  # a read-only file cannot be deleted on Windows until the flag is cleared (BUG-19)
                os.chmod(inner, stat_module.S_IWRITE)
                os.unlink(inner)
            except OSError as error:
                raise translate(error, inner) from None
        except OSError as error:
            raise translate(error, inner) from None

    def rmdir(self, inner: str) -> None:
        try:
            os.rmdir(inner)
        except PermissionError:
            if not WINDOWS:
                raise PermissionDenied("Permission denied", inner) from None
            try:
                os.chmod(inner, stat_module.S_IWRITE)
                os.rmdir(inner)
            except OSError as error:
                raise translate(error, inner) from None
        except OSError as error:
            raise translate(error, inner) from None

    def touch(self, inner: str) -> None:
        try:
            with open(inner, "ab"):
                pass
            os.utime(inner)
        except OSError as error:
            raise translate(error, inner) from None

    def join(self, inner: str, name: str) -> str:
        return os.path.join(inner, name)

    def parent(self, inner: str) -> str:
        return str(PurePath(inner).parent)
