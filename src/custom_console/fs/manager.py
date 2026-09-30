"""Navigation and file operations across the virtual tree.

The :class:`FileManager` hides the three storage backends (local disks/WSL,
reMarkable, virtual root) behind one API. It is independent of any display
layer (rich, prompt_toolkit, LLM tool results): every failure is reported with
a standard exception and each caller presents it to its own audience.

It never calls ``os.chdir``: the "current directory" is tracked per instance,
so several managers (the shell, the agent) can coexist without sharing state.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, List, NamedTuple, Optional, Tuple

from . import virtual
from .errors import (
    BinaryFileError,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    RemarkableUnavailableError,
    UnsafeOperationError,
)
from .remarkable import RemarkableBackend

ProgressCallback = Callable[[str], None]


class Backend(str, Enum):
    ROOT = "root"
    LOCAL = "local"
    REMOTE = "remarkable"


@dataclass(frozen=True)
class Target:
    """A resolved path: which backend owns it and its path inside that backend."""

    backend: Backend
    path: str


class Permissions(NamedTuple):
    readable: bool
    writable: bool
    executable: bool


@dataclass(frozen=True)
class ReadResult:
    text: str
    truncated: bool = False


def _normalize(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/")


def _compile(pattern: str) -> "re.Pattern[str]":
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


class FileManager:
    """Current location + operations over the local, WSL and reMarkable trees."""

    MODE_ROOT = Backend.ROOT.value
    MODE_LOCAL = Backend.LOCAL.value
    MODE_REMARKABLE = Backend.REMOTE.value

    def __init__(
        self,
        *,
        rmapi_path: "str | os.PathLike[str] | None" = None,
        wsl_distro: str = "Ubuntu",
        start_dir: Optional[str] = None,
        sync_dir: "str | Path | None" = None,
    ) -> None:
        self.wsl_distro = wsl_distro
        self.sync_dir = Path(sync_dir) if sync_dir else None
        self.local_location: str = _normalize(start_dir or os.getcwd())
        self.mode: str = self.MODE_LOCAL
        self.remarkable: Optional[RemarkableBackend] = (
            RemarkableBackend(rmapi_path)
            if rmapi_path and os.path.isfile(rmapi_path)
            else None
        )

    @classmethod
    def from_settings(cls, settings, start_dir: Optional[str] = None) -> "FileManager":
        return cls(
            rmapi_path=settings.rmapi_path,
            wsl_distro=settings.wsl_distro,
            start_dir=start_dir,
            sync_dir=settings.remarkable_sync_dir,
        )

    def clone(self) -> "FileManager":
        """Independent manager starting at the same location."""
        other = FileManager(
            rmapi_path=self.remarkable.exe_path if self.remarkable else None,
            wsl_distro=self.wsl_distro,
            start_dir=self.local_location,
            sync_dir=self.sync_dir,
        )
        other.mode = self.mode
        if self.remarkable and other.remarkable:
            other.remarkable.path = self.remarkable.path
        return other

    # -- state -------------------------------------------------------------- #

    @property
    def in_remarkable(self) -> bool:
        return self.mode == self.MODE_REMARKABLE

    @property
    def in_root(self) -> bool:
        return self.mode == self.MODE_ROOT

    @property
    def location(self) -> str:
        if self.mode == self.MODE_ROOT:
            return "/"
        if self.mode == self.MODE_REMARKABLE:
            return f"reMarkable:{self._remote().pwd()}"
        return self.local_location

    def virtual_root_entries(self) -> List[str]:
        return virtual.root_entries(self.wsl_distro)

    # -- guards ------------------------------------------------------------- #

    def _remote(self) -> RemarkableBackend:
        if self.remarkable is None:
            raise RemarkableUnavailableError(
                "reMarkable is not configured (RMAPI_PATH missing or invalid)"
            )
        return self.remarkable

    def _require_not_root(self, action: str) -> None:
        if self.mode == self.MODE_ROOT:
            raise IsVirtualRootError(
                f"'{action}' makes no sense on the virtual root '/'. "
                "Move to C:/, reMarkable/ or a WSL entry first."
            )

    @staticmethod
    def _reject_root(target: Target, action: str) -> None:
        if target.backend is Backend.ROOT:
            raise IsVirtualRootError(f"'{action}' makes no sense on the virtual root '/'.")

    # -- path resolution ---------------------------------------------------- #

    def _local(self, raw: str) -> str:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.local_location, expanded)
        return _normalize(expanded)

    def _from_virtual(self, path: virtual.VirtualPath) -> Target:
        if path.kind == "remarkable":
            self._remote()  # fail early when the tablet is not configured
            return Target(Backend.REMOTE, virtual.remote_path(path))
        return Target(Backend.LOCAL, virtual.local_path(path, self.wsl_distro))

    def _current(self) -> Target:
        if self.mode == self.MODE_ROOT:
            return Target(Backend.ROOT, "/")
        if self.mode == self.MODE_REMARKABLE:
            return Target(Backend.REMOTE, self._remote().pwd())
        return Target(Backend.LOCAL, self.local_location)

    def resolve(self, raw: str) -> Target:
        """Turn a user-typed path into a :class:`Target`.

        Accepts virtual absolute paths (``/C:/x``, ``/reMarkable/x``,
        ``/wsl-Ubuntu/x``), ``reMarkable:<path>``, ``~`` and paths relative to
        the current location (whichever backend that is).
        """
        raw = raw.strip()
        if raw in ("", "."):
            return self._current()
        if raw in ("/", "\\"):
            return Target(Backend.ROOT, "/")

        absolute = virtual.parse(raw, self.wsl_distro, absolute=True)
        if absolute is not None:
            return self._from_virtual(absolute)

        remote_ref = virtual.strip_remote_prefix(raw)
        if remote_ref is not None:
            return Target(Backend.REMOTE, self._remote().resolve(remote_ref))

        if raw == "~" or raw.startswith(("~/", "~\\")):
            return Target(Backend.LOCAL, self._local(raw))

        if self.mode == self.MODE_REMARKABLE:
            return Target(Backend.REMOTE, self._remote().resolve(raw))

        if self.mode == self.MODE_ROOT:
            relative = virtual.parse(raw, self.wsl_distro, absolute=False)
            if relative is None:
                raise FileNotFoundError(raw)
            return self._from_virtual(relative)

        return Target(Backend.LOCAL, self._local(raw))

    # -- low level directory access ---------------------------------------- #

    def _scan(self, target: Target) -> List[Tuple[str, bool]]:
        """Entries of a directory as ``(name, is_dir)``, for any backend."""
        if target.backend is Backend.ROOT:
            return [(entry.rstrip("/"), True) for entry in self.virtual_root_entries()]
        if target.backend is Backend.REMOTE:
            return self._remote().scandir(target.path)

        entries: List[Tuple[str, bool]] = []
        with os.scandir(target.path) as it:
            for entry in it:
                try:
                    entries.append((entry.name, entry.is_dir()))
                except OSError:
                    entries.append((entry.name, False))
        return entries

    def _require_dir(self, target: Target) -> None:
        if target.backend is Backend.LOCAL:
            if os.path.isfile(target.path):
                raise NotADirectoryError(target.path)
            if not os.path.isdir(target.path):
                raise FileNotFoundError(target.path)
        elif target.backend is Backend.REMOTE:
            if not self._remote().is_dir(target.path):
                raise NotADirectoryError(target.path)

    @staticmethod
    def _child(target: Target, name: str) -> Target:
        if target.backend is Backend.LOCAL:
            return Target(Backend.LOCAL, _normalize(os.path.join(target.path, name)))
        if target.backend is Backend.REMOTE:
            return Target(Backend.REMOTE, posixpath.join(target.path, name))
        return Target(Backend.ROOT, "/")

    def suggest(self, partial: str) -> List[Tuple[str, bool]]:
        """Completion candidates for a partially typed path.

        Returns ``(text, is_dir)`` pairs where ``text`` is the full replacement
        for `partial` (directory part included, no trailing slash). Failures
        (unknown directory, rmapi error...) yield an empty list.
        """
        normalized = partial.replace("\\", "/")
        cut = normalized.rfind("/") + 1
        directory, prefix = normalized[:cut], normalized[cut:]
        try:
            entries = self._scan(self.resolve(directory or "."))
        except Exception:
            return []
        lowered = prefix.lower()
        return sorted(
            (directory + name, is_dir)
            for name, is_dir in entries
            if name.lower().startswith(lowered)
        )

    # -- navigation --------------------------------------------------------- #

    def change_directory(self, path: str) -> None:
        target = self.resolve(path)

        if target.backend is Backend.ROOT:
            self.mode = self.MODE_ROOT
            return

        if target.backend is Backend.REMOTE:
            remote = self._remote()
            if target.path == "/":
                remote.path = "/"
            else:
                remote.cd(target.path)
            self.mode = self.MODE_REMARKABLE
            return

        self._require_dir(target)
        self.local_location = target.path
        self.mode = self.MODE_LOCAL

    # -- listing ------------------------------------------------------------ #

    def listdir(self, path: str = ".", show_hidden: bool = False) -> List[str]:
        """Names in a directory; sub-directories get a trailing ``/``."""
        target = self.resolve(path)
        self._require_dir(target)
        entries = self._scan(target)
        if not show_hidden:
            entries = [(n, d) for n, d in entries if not n.startswith(".")]
        if target.backend is not Backend.ROOT:
            entries = sorted(entries, key=lambda e: e[0].lower())
        return [name + "/" if is_dir else name for name, is_dir in entries]

    def tree(self, path: str = ".", depth: int = 3, show_hidden: bool = False) -> List[str]:
        """Lines of a directory tree. `depth` -1 means unlimited.

        Directories are shown with a trailing ``/``; a directory at the depth
        limit is followed by ``...`` when it has content.
        """
        target = self.resolve(path)
        self._reject_root(target, "tree")
        self._require_dir(target)
        limit = float("inf") if depth < 0 else depth

        label = posixpath.basename(target.path.rstrip("/")) or target.path
        lines = [label.rstrip("/") + "/" if label != "/" else "/"]

        def visible(entries):
            return [e for e in entries if show_hidden or not e[0].startswith(".")]

        def walk(node: Target, level: int) -> None:
            indent = "    " * (level + 1)
            entries = visible(self._scan(node))
            if level >= limit:
                if entries:
                    lines.append(f"{indent}...")
                return
            for name in sorted((n for n, d in entries if not d), key=str.lower):
                lines.append(f"{indent}{name}")
            for name in sorted((n for n, d in entries if d), key=str.lower):
                lines.append(f"{indent}{name}/")
                try:
                    walk(self._child(node, name), level + 1)
                except PermissionError:
                    lines.append(f"{indent}    (permission denied)")

        walk(target, 0)
        return lines

    # -- find --------------------------------------------------------------- #

    def _walk(self, node: Target, prefix: str, depth: int) -> Iterator[Tuple[str, str, bool]]:
        """Yield ``(display_path, name, is_dir)`` for entries below `node`."""
        if depth == 0:
            return
        try:
            entries = self._scan(node)
        except PermissionError:
            return
        except OSError:
            if node.backend is Backend.LOCAL:
                return
            raise
        for name, is_dir in entries:
            display = f"{prefix}{name}"
            yield display, name, is_dir
            if is_dir:
                yield from self._walk(self._child(node, name), display + "/", depth - 1)

    def find(
        self,
        pattern: str,
        path: str = ".",
        depth: int = 10,
        strict: bool = False,
    ) -> List[str]:
        """Entries whose name matches the regex `pattern` (case-insensitive).

        A trailing ``/`` in `pattern` restricts the search to directories.
        `strict` requires the whole name to match. `depth` 1 only looks at the
        direct children of `path`.
        """
        if not pattern:
            raise ValueError("find: pattern must not be empty.")

        dir_only = pattern[-1] in "/\\"
        regex = _compile(pattern[:-1] if dir_only else pattern)

        target = self.resolve(path)
        self._reject_root(target, "find")
        self._require_dir(target)

        # Local results are shown relative to what the user typed.
        if target.backend is Backend.LOCAL:
            typed = path.strip().replace("\\", "/")
            prefix = "" if typed in ("", ".") else typed.rstrip("/") + "/"
        else:
            prefix = target.path.rstrip("/") + "/"

        results = []
        for display, name, is_dir in self._walk(target, prefix, depth):
            if dir_only and not is_dir:
                continue
            if regex.fullmatch(name) if strict else regex.search(name):
                results.append(display)
        return sorted(results)

    # -- read / stat -------------------------------------------------------- #

    def read(self, path: str, max_bytes: Optional[int] = None) -> ReadResult:
        """Read a text file. Raises BinaryFileError for binary content."""
        self._require_not_root("cat")
        if self.mode == self.MODE_REMARKABLE:
            raise InReMarkableError(
                "Cannot display a reMarkable file directly (notebooks are not text)."
            )

        target = self.resolve(path)
        self._reject_root(target, "cat")
        if target.backend is Backend.REMOTE:
            raise InReMarkableError(
                "Cannot display a reMarkable file directly (notebooks are not text)."
            )
        if os.path.isdir(target.path):
            raise NotAFileError(target.path)
        if not os.path.isfile(target.path):
            raise FileNotFoundError(target.path)

        with open(target.path, "rb") as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if b"\x00" in data[:8192]:
            raise BinaryFileError(f"{path}: binary file")

        truncated = max_bytes is not None and len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        # Normalize Windows line endings, as text mode would.
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return ReadResult(text, truncated)

    def cat(self, path: str) -> str:
        return self.read(path).text

    def stat(self, path: str) -> Permissions:
        self._require_not_root("stat")
        target = self.resolve(path)
        self._reject_root(target, "stat")

        if target.backend is Backend.REMOTE:
            self._remote().is_dir(target.path)  # raises FileNotFoundError if absent
            return Permissions(True, True, True)

        if not os.path.exists(target.path):
            raise FileNotFoundError(target.path)
        return Permissions(
            os.access(target.path, os.R_OK),
            os.access(target.path, os.W_OK),
            os.access(target.path, os.X_OK),
        )

    # -- copy --------------------------------------------------------------- #

    def copy(
        self,
        src: str,
        dst: str,
        recursive: bool = False,
        on_file: Optional[ProgressCallback] = None,
    ) -> int:
        """Copy `src` to `dst` (like ``cp``), across backends. Returns the number
        of files copied; `on_file` is called with each copied file.

        - local -> local: files, or folders with `recursive`. When `dst` is an
          existing folder the source is copied *into* it.
        - reMarkable -> local: `dst` is the destination folder (rmapi keeps the
          document name).
        - local -> reMarkable: files only; uploaded into the folder of `dst`.
        - reMarkable -> reMarkable: files only (through a temporary folder).
        """
        self._require_not_root("cp")
        source, dest = self.resolve(src), self.resolve(dst)
        self._reject_root(source, "cp")
        self._reject_root(dest, "cp")

        pair = (source.backend, dest.backend)
        if pair == (Backend.LOCAL, Backend.LOCAL):
            return self._copy_local(source.path, dest.path, recursive, on_file)
        if pair == (Backend.REMOTE, Backend.LOCAL):
            return self._download(source.path, dest.path, recursive, on_file)
        if pair == (Backend.LOCAL, Backend.REMOTE):
            return self._upload(source.path, dest.path, on_file)
        return self._copy_remote(source.path, dest.path, on_file)

    @staticmethod
    def _notify(callback: Optional[ProgressCallback], path: str) -> None:
        if callback is not None:
            callback(path)

    def _copy_local(
        self, src: str, dst: str, recursive: bool, on_file: Optional[ProgressCallback]
    ) -> int:
        if os.path.isdir(src):
            if not recursive:
                raise ValueError(f"-r not specified; omitting directory '{src}'")
            target = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
            real_src = os.path.normcase(os.path.realpath(src))
            real_dst = os.path.normcase(os.path.realpath(target))
            if real_dst == real_src or real_dst.startswith(real_src + os.sep):
                raise ValueError(f"cannot copy '{src}' into itself")
            return self._copy_tree(src, target, on_file)

        if os.path.isfile(src):
            target = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
            shutil.copy2(src, target)
            self._notify(on_file, src)
            return 1

        raise FileNotFoundError(src)

    def _copy_tree(self, src: str, dst: str, on_file: Optional[ProgressCallback]) -> int:
        count = 0
        os.makedirs(dst, exist_ok=True)
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_root = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_root, exist_ok=True)
            for name in files:
                source_file = os.path.join(root, name)
                shutil.copy2(source_file, os.path.join(target_root, name))
                count += 1
                self._notify(on_file, source_file)
        return count

    def _download(
        self, src: str, dst: str, recursive: bool, on_file: Optional[ProgressCallback]
    ) -> int:
        remote = self._remote()
        if remote.is_dir(src):
            if not recursive:
                raise ValueError(f"-r not specified; omitting directory '{src}'")
            local_dst = dst
            if os.path.isdir(dst):
                local_dst = _normalize(
                    os.path.join(dst, posixpath.basename(src.rstrip("/")) or "reMarkable")
                )
            os.makedirs(local_dst, exist_ok=True)
            return remote.download_tree(src, local_dst, on_file)

        remote.get(src, dst)
        self._notify(on_file, src)
        return 1

    def _remote_folder(self, dst: str) -> str:
        """Folder receiving an upload aimed at `dst` (rmapi keeps the file name)."""
        remote = self._remote()
        try:
            if remote.is_dir(dst):
                return dst
        except FileNotFoundError:
            pass
        return posixpath.dirname(dst) or "/"

    def _upload(self, src: str, dst: str, on_file: Optional[ProgressCallback]) -> int:
        if os.path.isdir(src):
            raise ValueError("Uploading a folder to the reMarkable is not supported.")
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        self._remote().put(src, self._remote_folder(dst))
        self._notify(on_file, src)
        return 1

    def _copy_remote(self, src: str, dst: str, on_file: Optional[ProgressCallback]) -> int:
        remote = self._remote()
        if remote.is_dir(src):
            raise ValueError(
                "Copying a folder within the tablet is not supported (rmapi can neither "
                "download nor upload a whole folder); export it with "
                "`cp -r reMarkable:/folder <local dir>` instead."
            )
        tmp_dir = tempfile.mkdtemp(prefix="rmapi_cp_")
        try:
            remote.get(src, tmp_dir)
            downloaded = os.listdir(tmp_dir)
            if not downloaded:
                raise FileNotFoundError(src)
            remote.put(os.path.join(tmp_dir, downloaded[0]), self._remote_folder(dst))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._notify(on_file, src)
        return 1

    # -- remove ------------------------------------------------------------- #

    def _assert_removable(self, target: Target) -> None:
        """Refuse the removals that can only be mistakes (roots, home, cwd...)."""
        if target.backend is Backend.REMOTE:
            if target.path == "/":
                raise UnsafeOperationError("Refusing to delete the whole reMarkable.")
            return

        resolved = Path(target.path).resolve()
        if resolved.parent == resolved:
            raise UnsafeOperationError(f"Refusing to delete the root '{target.path}'.")
        if resolved == Path.home().resolve():
            raise UnsafeOperationError("Refusing to delete your home folder.")
        location = Path(self.local_location).resolve()
        if location == resolved or resolved in location.parents:
            raise UnsafeOperationError(
                f"Refusing to delete '{target.path}': it contains the current directory."
            )

    def remove(self, path: str, recursive: bool = False) -> None:
        """Delete a file, or a folder (`recursive` for non-empty ones)."""
        self._require_not_root("rm")
        target = self.resolve(path)
        self._reject_root(target, "rm")
        self._assert_removable(target)

        if target.backend is Backend.REMOTE:
            self._remote().remove(target.path, recursive=recursive)
            return

        if not os.path.lexists(target.path):
            raise FileNotFoundError(target.path)
        if os.path.islink(target.path) or not os.path.isdir(target.path):
            os.remove(target.path)
        elif recursive:
            shutil.rmtree(target.path)
        else:
            os.rmdir(target.path)
