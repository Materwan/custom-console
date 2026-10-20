"""The reMarkable tablet, through the `rmapi` command line tool, behind the backend interface.

`rmapi` is a third-party tool on an unofficial service, so everything that knows it is here and nowhere else; the converter
(`rmdoc.py`) is a separate module. One-shot mode is used (`rmapi <command> <args>`), because the interactive mode needs a
real terminal. Every call: input closed (a tool waiting for a code gets an end of file instead of hanging), a timeout, and a
readable message for each failure: not installed, not signed in, cloud unreachable, file not found.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ...platform import CancelToken
from ...platform.process import StartError, run_capture
from .backend import (
    AlreadyExists,
    BackendFailure,
    DirectoryNotEmpty,
    Entry,
    NotFound,
    Stat,
    Unsupported,
)

LISTING = re.compile(r"^\s*\[(f|d)\]\s+(.+)$")
ERROR_WORD = re.compile(r"\bERROR\b")
NOT_FOUND = re.compile(r"(?i)(doesn'?t|does not|do not|not)\s+exist|not found|no such|cannot find")
NOT_EMPTY = re.compile(r"(?i)not empty")
EXISTS = re.compile(r"(?i)already exists?|entry already")
UNREACHABLE = re.compile(r"(?i)dial tcp|no such host|connection refused|i/o timeout|tls handshake|network is unreachable|"
                         r"connection reset|temporary failure in name resolution|timeout awaiting")
SIGN_IN = re.compile(r"(?i)one-time code|enter code|authentication required")

MIN_INTERVAL = 0.3  # seconds between calls: each one-shot call creates a cloud token, and going faster brings HTTP 429
RETRIES = 6
FIRST_BACKOFF = 2.0
CALL_TIMEOUT = 120.0
TRANSFER_TIMEOUT = 600.0
CACHE_SECONDS = 20.0
DOCUMENT_SUFFIXES = (".pdf", ".epub", ".rmdoc", ".zip")


class RmapiError(Exception):
    """A failed rmapi call. `kind` is one of: missing, signin, unreachable, ratelimit, notfound, exists, notempty, failed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def status_lines(output: str) -> list[str]:
    """The lines of rmapi output that are not folder listing entries. Listing entries carry names chosen by the user, so they
    must never be scanned for error words or HTTP codes (BUG-17)."""
    return [line for line in output.splitlines() if line.strip() and not LISTING.match(line)]


def parse_listing(output: str) -> list[Entry]:
    """`[f]  name` / `[d]  name` lines. A name is kept verbatim: documents may end with a space, and stripping it would make
    later `get` and `put` calls fail."""
    entries: list[Entry] = []
    for line in output.splitlines():
        match = LISTING.match(line)
        if match:
            entries.append(Entry(match.group(2), match.group(1) == "d"))
    return entries


def classify(output: str, returncode: int | None) -> RmapiError | None:
    """The error in an rmapi answer, judged from its status lines only; None when the call went well."""
    lines = status_lines(output)
    for line in lines:
        if SIGN_IN.search(line):
            return RmapiError("signin", "rmapi is not signed in: run `rmapi` once in a terminal and enter the code it asks for.")
    errors = [line.strip() for line in lines if ERROR_WORD.search(line)]
    if any("429" in line or "too many requests" in line.lower() for line in lines):
        return RmapiError("ratelimit", "The reMarkable cloud is limiting requests (HTTP 429).")
    if errors:
        text = errors[0]
        if UNREACHABLE.search(text):  # first: "no such host" is a network failure, not a missing file
            return RmapiError("unreachable", "The reMarkable cloud cannot be reached (check the network).")
        if NOT_FOUND.search(text):
            return RmapiError("notfound", "No such file or directory")
        if NOT_EMPTY.search(text):
            return RmapiError("notempty", "Directory not empty")
        if EXISTS.search(text):
            return RmapiError("exists", "Already exists")
        return RmapiError("failed", text.removeprefix("ERROR:").removeprefix("ERROR").strip() or text)
    if returncode not in (0, None):
        detail = f": {lines[0].strip()}" if lines else ""
        return RmapiError("failed", f"rmapi exited with code {returncode}{detail}")
    return None


class RmapiRunner:
    """Runs `rmapi`, one call at a time, politely."""

    def __init__(self, command: Sequence[str], *, timeout: float = CALL_TIMEOUT, min_interval: float = MIN_INTERVAL,
                 retries: int = RETRIES, first_backoff: float = FIRST_BACKOFF, env: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.command = list(command)
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self.first_backoff = first_backoff
        self.env = env
        self._clock = clock
        self._last = 0.0
        self.activity: Callable[[str | None], None] | None = None  # the interface's spinner: a label while a call runs

    def _pause(self, seconds: float, cancel: CancelToken | None) -> None:
        if seconds <= 0:
            return
        if cancel is not None:
            if cancel.wait(seconds):
                cancel.raise_if_cancelled()
        else:
            time.sleep(seconds)

    def run(self, *args: str, cwd: str | None = None, cancel: CancelToken | None = None, timeout: float | None = None) -> str:
        """The output of `rmapi <args>`. Raises RmapiError. A 429 is retried with a doubling delay."""
        delay = self.first_backoff
        label = " ".join(args[:2])
        if self.activity:
            self.activity(f"rmapi {label}")
        try:
            for attempt in range(1, self.retries + 1):
                self._pause(self.min_interval - (self._clock() - self._last), cancel)
                try:
                    done = run_capture([*self.command, *args], cwd=cwd, env=self.env, timeout=timeout or self.timeout, cancel=cancel)
                except StartError:
                    raise RmapiError("missing", "rmapi is not installed (or CONSOLE_RMAPI points nowhere). Get it from "
                                                "github.com/ddvk/rmapi and sign in once.") from None
                self._last = self._clock()
                if cancel is not None:
                    cancel.raise_if_cancelled()
                if done.timed_out:
                    raise RmapiError("unreachable", f"rmapi did not answer in {timeout or self.timeout:.0f} seconds.")
                problem = classify(done.output, done.returncode)
                if problem is None:
                    return done.output
                if problem.kind == "ratelimit" and attempt < self.retries:
                    self._pause(delay, cancel)
                    delay *= 2
                    continue
                if problem.kind == "ratelimit":
                    raise RmapiError("ratelimit", f"The reMarkable cloud is still limiting requests after {self.retries} attempts.")
                raise problem
            raise RmapiError("failed", "rmapi gave no answer")  # pragma: no cover
        finally:
            if self.activity:
                self.activity(None)


def _vfs_error(error: RmapiError, path: str) -> Exception:
    if error.kind == "notfound":
        return NotFound(error.message, path)
    if error.kind == "exists":
        return AlreadyExists(error.message, path)
    if error.kind == "notempty":
        return DirectoryNotEmpty(error.message, path)
    return BackendFailure(error.message, path)


class RemarkableBackend:
    name = "remarkable"
    label = "the reMarkable"
    caps = frozenset({"list", "stat", "read", "write", "mkdir", "rename", "remove", "rmdir"})

    def __init__(self, runner: RmapiRunner, *, cache_seconds: float = CACHE_SECONDS, clock: Callable[[], float] = time.monotonic,
                 temp_root: str | None = None):
        self.runner = runner
        self._ttl = cache_seconds
        self._clock = clock
        self._temp_root = temp_root
        self._cache: dict[str, tuple[float, list[Entry]]] = {}

    # ------------------------------------------------------------------ plumbing
    def _call(self, *args: str, path: str, cancel: CancelToken | None = None, cwd: str | None = None,
              timeout: float | None = None) -> str:
        try:
            return self.runner.run(*args, cwd=cwd, cancel=cancel, timeout=timeout)
        except RmapiError as error:
            raise _vfs_error(error, path) from None

    def forget(self) -> None:
        """Drop the cached listings (any change on the tablet calls this)."""
        self._cache.clear()

    @staticmethod
    def _norm(inner: str) -> str:
        return posixpath.normpath("/" + inner.lstrip("/"))

    # ------------------------------------------------------------------ reading
    def list(self, inner: str, cancel: CancelToken | None = None) -> list[Entry]:
        path = self._norm(inner)
        cached = self._cache.get(path)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return list(cached[1])
        output = self._call("ls", path, path=path, cancel=cancel)
        entries = parse_listing(output)
        if path != "/" and len(entries) == 1 and not entries[0].is_dir and entries[0].name == posixpath.basename(path):
            raise _vfs_error(RmapiError("failed", "Not a directory"), path)  # `ls` of a document lists the document itself
        self._cache[path] = (self._clock(), entries)
        return list(entries)

    def stat(self, inner: str) -> Stat | None:
        path = self._norm(inner)
        if path == "/":
            return Stat(is_dir=True)
        parent, name = posixpath.split(path)
        try:
            siblings = self.list(parent)
        except NotFound:
            return None
        for entry in siblings:
            if entry.name == name:
                return Stat(is_dir=entry.is_dir)
        return None

    def export_name(self, inner: str) -> str:
        """The name a document gets when it is copied off the tablet."""
        return posixpath.basename(self._norm(inner)) + ".rmdoc"

    def read(self, inner: str, max_bytes: int | None = None, cancel: CancelToken | None = None) -> bytes:
        """Download a document: its `.rmdoc` archive."""
        path = self._norm(inner)
        folder = tempfile.mkdtemp(prefix="console-rm-", dir=self._temp_root)
        try:
            self._call("get", path, path=path, cancel=cancel, cwd=folder, timeout=TRANSFER_TIMEOUT)
            files = [f for f in Path(folder).iterdir() if f.is_file()]
            if not files:
                raise BackendFailure("rmapi downloaded nothing", path)
            data = files[0].read_bytes()
            return data if max_bytes is None else data[:max_bytes]
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # ------------------------------------------------------------------ changing
    @staticmethod
    def _suffix_for(data: bytes, name: str) -> str:
        known = posixpath.splitext(name)[1].lower()
        if known in DOCUMENT_SUFFIXES:
            return known
        if data.startswith(b"%PDF"):
            return ".pdf"
        if data.startswith(b"PK"):
            return ".rmdoc"
        raise Unsupported("Only PDF, EPUB and .rmdoc files can be uploaded to the reMarkable.", name)

    def write(self, inner: str, data: bytes, cancel: CancelToken | None = None) -> None:
        """Upload a document. The document is named after the destination (without its extension). An existing document of
        that name is replaced only after the new one is safely up."""
        path = self._norm(inner)
        parent, name = posixpath.split(path)
        suffix = self._suffix_for(data, name)
        stem = name[: -len(suffix)] if name.lower().endswith(suffix) else name
        document = posixpath.join(parent or "/", stem)  # the tablet names a document without its extension
        existing = self.stat(document)
        if existing is not None and existing.is_dir:
            raise AlreadyExists("Is a folder", document)
        staging = stem if existing is None else f"{stem}.upload-{int(time.time())}"
        folder = tempfile.mkdtemp(prefix="console-rm-", dir=self._temp_root)
        try:
            local = Path(folder) / f"{staging}{suffix}"
            local.write_bytes(data)
            self._call("put", str(local), parent or "/", path=path, cancel=cancel, timeout=TRANSFER_TIMEOUT)
            if existing is not None:
                self._call("rm", document, path=document, cancel=cancel)
                self._call("mv", posixpath.join(parent or "/", staging), document, path=document, cancel=cancel)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            self.forget()

    def mkdir(self, inner: str, parents: bool = False) -> None:
        path = self._norm(inner)
        if parents:
            current = "/"
            for part in [p for p in path.split("/") if p]:
                current = posixpath.join(current, part)
                if self.stat(current) is None:
                    self._call("mkdir", current, path=current)
                    self.forget()
            return
        self._call("mkdir", path, path=path)
        self.forget()

    def rename(self, source: str, destination: str) -> None:
        src, dst = self._norm(source), self._norm(destination)
        self._call("mv", src, dst, path=src)
        self.forget()

    def remove(self, inner: str) -> None:
        path = self._norm(inner)
        self._call("rm", path, path=path)
        self.forget()

    def rmdir(self, inner: str) -> None:
        self.remove(inner)

    def touch(self, inner: str) -> None:
        raise Unsupported("A file cannot be touched on the reMarkable.", inner)

    def join(self, inner: str, name: str) -> str:
        return posixpath.join(self._norm(inner), name)

    def parent(self, inner: str) -> str:
        return posixpath.dirname(self._norm(inner)) or "/"


def find_rmapi(configured: str | None) -> list[str] | None:
    """The command that starts rmapi: the configured path, else `rmapi` on the PATH; None when neither exists."""
    if configured:
        return [configured] if os.path.exists(configured) or shutil.which(configured) else None
    found = shutil.which("rmapi")
    return [found] if found else None

