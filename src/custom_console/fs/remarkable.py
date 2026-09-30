"""reMarkable backend, built on the `rmapi` command line tool."""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .errors import RemarkableError, UnsafeOperationError

Entry = Tuple[str, bool]  # (name, is_directory)

_LISTING_PREFIXES = ("[f]", "[d]")
_LISTING_RE = re.compile(r"^\s*\[(f|d)\]\s+(.+)$")
_ERROR_RE = re.compile(r"\bERROR\b")
_NOT_FOUND_RE = re.compile(
    r"(?i)(doesn'?t|does not|do not|not)\s+exist|not found|no such|cannot find"
)


def _status_lines(output: str) -> List[str]:
    """Lines of rmapi output that are not directory listing entries.

    Listing entries (``[f] name`` / ``[d] name``) carry user-chosen document
    names, so they must never be scanned for error markers or HTTP codes.
    """
    return [
        line
        for line in output.splitlines()
        if line.strip() and not line.lstrip().startswith(_LISTING_PREFIXES)
    ]


def first_error_line(output: str) -> Optional[str]:
    for line in _status_lines(output):
        if _ERROR_RE.search(line):
            return line.strip()
    return None


def is_rate_limited(output: str) -> bool:
    return any("429" in line for line in _status_lines(output))


def parse_listing(output: str) -> List[Entry]:
    """Parse ``rmapi ls`` output, one entry per line: ``[f]  name`` / ``[d]  name``."""
    entries: List[Entry] = []
    for line in output.splitlines():
        # `splitlines` already dropped the line terminators; the name is kept
        # verbatim because documents may end with a space, and stripping it
        # would make later `get`/`put` calls fail.
        match = _LISTING_RE.match(line)
        if match:
            entries.append((match.group(2), match.group(1) == "d"))
    return entries


class RemarkableBackend:
    """Wrapper around ``rmapi`` in one-shot mode (``rmapi <cmd> <args>``).

    The interactive mode needs a real TTY, so every call spawns a new process.
    rmapi keeps no state between two calls: the "current remote folder" is
    tracked here and combined with the requested path before each command.
    """

    # Minimum delay (s) between two invocations: each one-shot call creates a
    # cloud auth token, and chaining them too fast triggers HTTP 429.
    MIN_CALL_INTERVAL = 0.3
    MAX_RETRY_ATTEMPTS = 6
    INITIAL_RETRY_DELAY = 2.0
    COMMAND_TIMEOUT = 120.0  # seconds, per rmapi invocation

    def __init__(self, exe_path: "str | os.PathLike[str]"):
        self.exe_path = str(exe_path)
        self.path = "/"  # current remote folder, posix style
        self._last_call_at = 0.0
        self.failed_downloads: List[str] = []

    # -- process ------------------------------------------------------------ #

    def _run(self, *args: str, cwd: Optional[str] = None) -> str:
        delay = self.INITIAL_RETRY_DELAY
        output = ""
        for attempt in range(1, self.MAX_RETRY_ATTEMPTS + 1):
            wait = self.MIN_CALL_INTERVAL - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)

            try:
                proc = subprocess.run(
                    [self.exe_path, *args],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=cwd,
                    timeout=self.COMMAND_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError(
                    f"rmapi {args[0] if args else ''} timed out after "
                    f"{self.COMMAND_TIMEOUT:.0f}s"
                ) from None
            self._last_call_at = time.monotonic()
            output = proc.stdout + proc.stderr

            if is_rate_limited(output):
                if attempt < self.MAX_RETRY_ATTEMPTS:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RemarkableError(
                    f"reMarkable cloud rate limit (HTTP 429) still active after "
                    f"{self.MAX_RETRY_ATTEMPTS} attempts"
                )
            return output
        return output

    @staticmethod
    def _raise_for_error(output: str, target: str) -> None:
        line = first_error_line(output)
        if line is None:
            return
        if _NOT_FOUND_RE.search(line):
            raise FileNotFoundError(target)
        raise RemarkableError(line)

    # -- paths -------------------------------------------------------------- #

    def resolve(self, subpath: str) -> str:
        """Combine the current folder with `subpath` (handles '.', '..', '/abs')."""
        if subpath in (".", ""):
            base = self.path
        elif subpath.startswith("/"):
            base = subpath
        else:
            base = posixpath.join(self.path, subpath)
        return posixpath.normpath(base) or "/"

    def pwd(self) -> str:
        return self.path

    # -- queries ------------------------------------------------------------ #

    def scandir(self, subpath: str = ".") -> List[Entry]:
        """Entries of a remote folder as ``(name, is_dir)``."""
        target = self.resolve(subpath)
        output = self._run("ls", target)
        self._raise_for_error(output, target)
        return parse_listing(output)

    def listdir(self, subpath: str = ".") -> List[str]:
        """Names of a remote folder's entries (directories get a trailing '/')."""
        return [name + "/" if is_dir else name for name, is_dir in self.scandir(subpath)]

    def is_dir(self, subpath: str) -> bool:
        """Whether `subpath` is a remote folder. Raises FileNotFoundError if absent."""
        target = self.resolve(subpath)
        if target == "/":
            return True
        parent = posixpath.dirname(target) or "/"
        basename = posixpath.basename(target)
        for name, is_dir in self.scandir(parent):
            if name == basename:
                return is_dir
        raise FileNotFoundError(target)

    def cd(self, subpath: str) -> None:
        target = self.resolve(subpath)
        entries = self.scandir(target)

        # rmapi lists a folder's content, but for a file it lists the file
        # itself: a single *file* entry named like the target means "not a folder".
        basename = posixpath.basename(target)
        if len(entries) == 1 and entries[0] == (basename, False):
            raise NotADirectoryError(target)

        self.path = target

    # -- transfers ---------------------------------------------------------- #

    def get(self, filename: str, dest: str = ".") -> None:
        """Download a remote file into the local folder `dest`.

        rmapi always downloads into its working directory, so the sub-process'
        cwd is set to `dest` (it has no destination argument).
        """
        target = self.resolve(filename)
        os.makedirs(dest, exist_ok=True)
        output = self._run("get", target, cwd=dest)
        self._raise_for_error(output, target)

    def put(self, local_path: str, remote_dir: str = ".") -> None:
        """Upload a local file into the remote folder `remote_dir`."""
        target = self.resolve(remote_dir)
        output = self._run("put", local_path, target)
        self._raise_for_error(output, target)

    def remove(self, subpath: str, recursive: bool = False) -> None:
        target = self.resolve(subpath)
        if target == "/":
            raise UnsafeOperationError("Refusing to delete the whole reMarkable.")
        if recursive and self.is_dir(target):
            self._remove_tree(target)
        else:
            self._remove_one(target)

    def _remove_one(self, target: str) -> None:
        output = self._run("rm", target)
        line = first_error_line(output)
        if line is not None:
            raise RemarkableError(f"Cannot delete {target}: {line}")

    def _remove_tree(self, target: str) -> None:
        for name, is_dir in self.scandir(target):
            child = posixpath.join(target, name)
            if is_dir:
                self._remove_tree(child)
            else:
                self._remove_one(child)
        self._remove_one(target)

    def download_tree(
        self,
        remote_path: str,
        local_path: str,
        on_file: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Recursively download `remote_path` into `local_path`.

        Files that fail to download are skipped and recorded in
        ``failed_downloads``. Returns the number of files downloaded.
        """
        count = 0
        for name, is_dir in self.scandir(remote_path):
            remote_entry = posixpath.join(remote_path, name)
            local_entry = os.path.join(local_path, name)
            if is_dir:
                os.makedirs(local_entry, exist_ok=True)
                count += self.download_tree(remote_entry, local_entry, on_file)
                continue
            try:
                time.sleep(0.1)
                self.get(remote_entry, dest=local_path)
            except (FileNotFoundError, RemarkableError):
                self.failed_downloads.append(remote_entry)
                continue
            count += 1
            if on_file is not None:
                on_file(remote_entry)
        return count

    def sync(
        self,
        dest: "str | Path",
        on_file: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Download the whole tablet into `dest`. Returns the file count."""
        os.makedirs(dest, exist_ok=True)
        return self.download_tree("/", str(dest), on_file)
