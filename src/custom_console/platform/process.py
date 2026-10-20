"""Running other programs: captured with a timeout, in the background, or attached to the terminal.

Every child starts with its input closed (unless text is given), so a program that asks a question gets an end of
file instead of waiting for ever. A child is started in its own process group, and a stop kills the whole tree.
"""

from __future__ import annotations

import contextlib
import locale
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .base import CancelToken

WINDOWS = sys.platform == "win32"
POLL = 0.05
GRACE = 2.0  # seconds a stopped process gets to end before it is killed
DEFAULT_KEEP = 2_000_000  # bytes of output kept per stream in memory (head and tail share it)


class StartError(Exception):
    """The program could not be started (not found, not executable, ...)."""


def decode(data: bytes) -> str:
    """UTF-8 when the bytes are, else the system's encoding; never an error."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


class HeadTail:
    """The first and the last bytes of a stream, and how many were dropped between them."""

    def __init__(self, keep: int = DEFAULT_KEEP):
        self.head_size = keep // 2
        self.tail_size = keep - self.head_size
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self.dropped = 0
        self.total = 0
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self.total += len(data)
            room = self.head_size - len(self._head)
            if room > 0:
                self._head += data[:room]
                data = data[room:]
            if not data:
                return
            self._tail.append(data)
            self._tail_bytes += len(data)
            while self._tail_bytes > self.tail_size:
                first = self._tail[0]
                excess = self._tail_bytes - self.tail_size
                if len(first) <= excess:
                    self._tail.popleft()
                    self._tail_bytes -= len(first)
                    self.dropped += len(first)
                else:
                    self._tail[0] = first[excess:]
                    self._tail_bytes -= excess
                    self.dropped += excess

    def text(self) -> str:
        with self._lock:
            head, tail = bytes(self._head), b"".join(self._tail)
            dropped = self.dropped
        if not dropped:
            return decode(head + tail)
        return f"{decode(head)}\n[... {dropped:,} bytes left out ...]\n{decode(tail)}"


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int | None
    output: str
    timed_out: bool = False
    cancelled: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


def _popen(args: Sequence[str] | str, *, shell: bool, cwd: str | None, env: Mapping[str, str] | None,
           stdin: int | None, merge: bool) -> subprocess.Popen[bytes]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # Windows only; its own group lets us stop the tree
    name = args if isinstance(args, str) else args[0]
    try:
        return subprocess.Popen(
            args, shell=shell, cwd=cwd, env=dict(env) if env is not None else None, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            creationflags=flags, start_new_session=not WINDOWS)
    except FileNotFoundError as error:
        raise StartError(f"{name}: not found ({error.filename or name})") from None
    except PermissionError:
        raise StartError(f"{name}: permission denied") from None
    except OSError as error:
        raise StartError(f"{name}: {error.strerror or error}") from None


def kill_tree(proc: subprocess.Popen[bytes], grace: float = GRACE) -> None:
    """End a process and everything it started: politely first, then for good."""
    if proc.poll() is not None:
        return
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace)


def _pump(stream: object, sink: HeadTail) -> threading.Thread:
    def run() -> None:
        read = stream.read1 if hasattr(stream, "read1") else stream.read  # type: ignore[attr-defined]
        with contextlib.suppress(OSError, ValueError):
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                sink.write(chunk)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def run_capture(args: Sequence[str] | str, *, shell: bool = False, cwd: str | None = None,
                env: Mapping[str, str] | None = None, timeout: float | None = None, input_text: str | None = None,
                cancel: CancelToken | None = None, merge: bool = True, keep: int = DEFAULT_KEEP) -> Completed:
    """Run a program and collect its output (stderr merged into it by default). Stops at the timeout or on cancel."""
    started = time.monotonic()
    proc = _popen(args, shell=shell, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                  merge=merge)
    out, err = HeadTail(keep), HeadTail(keep)
    assert proc.stdout is not None
    threads = [_pump(proc.stdout, out)]
    if proc.stderr is not None:
        threads.append(_pump(proc.stderr, err))
    if input_text is not None and proc.stdin is not None:
        def feed() -> None:
            with contextlib.suppress(OSError, ValueError):
                assert proc.stdin is not None
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
        threading.Thread(target=feed, daemon=True).start()
    timed_out = cancelled = False
    deadline = None if timeout is None else started + timeout
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.cancelled:
                cancelled = True
            elif deadline is not None and time.monotonic() >= deadline:
                timed_out = True
            if cancelled or timed_out:
                kill_tree(proc)
                break
            time.sleep(POLL)
    except BaseException:  # Ctrl+C while waiting: never leave the child behind
        kill_tree(proc)
        raise
    for thread in threads:
        thread.join(timeout=GRACE)
    text = out.text()
    if proc.stderr is not None and err.total:
        text = f"{text}\n{err.text()}" if text else err.text()
    return Completed(proc.returncode, text, timed_out, cancelled, time.monotonic() - started)


def run_attached(args: Sequence[str] | str, *, cwd: str | None = None, env: Mapping[str, str] | None = None,
                 shell: bool = False) -> int:
    """Run a program on the terminal, as the user typed it. Returns its exit code (130 when interrupted)."""
    try:
        proc = subprocess.Popen(args, shell=shell, cwd=cwd, env=dict(env) if env is not None else None)
    except FileNotFoundError:
        name = args if isinstance(args, str) else args[0]
        raise StartError(f"{name}: command not found") from None
    except PermissionError:
        name = args if isinstance(args, str) else args[0]
        raise StartError(f"{name}: permission denied") from None
    except OSError as error:
        name = args if isinstance(args, str) else args[0]
        raise StartError(f"{name}: {error.strerror or error}") from None
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            return proc.wait(timeout=GRACE)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            kill_tree(proc, grace=0.5)
            return 130


class BackgroundJob:
    """A command running while the conversation goes on: its output is read as it comes."""

    def __init__(self, command: str, proc: subprocess.Popen[bytes], buffer: HeadTail):
        self.id = uuid.uuid4().hex[:8]
        self.command = command
        self.started = time.monotonic()
        self._proc = proc
        self._buffer = buffer
        self._finished_at: float | None = None
        self._stopped = False
        self._threads: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        if self._proc.poll() is None:
            return True
        if self._finished_at is None:
            self._finished_at = time.monotonic()
        return False

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def seconds(self) -> float:
        end = self._finished_at if self._finished_at is not None else time.monotonic()
        return end - self.started

    def output(self) -> str:
        return self._buffer.text()

    def stop(self) -> None:
        self._stopped = True
        kill_tree(self._proc)

    def wait(self, timeout: float | None = None) -> bool:
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        for thread in self._threads:
            thread.join(timeout=GRACE)
        return True


def start_background(command: str, *, cwd: str | None = None, env: Mapping[str, str] | None = None,
                     keep: int = DEFAULT_KEEP) -> BackgroundJob:
    proc = _popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, merge=True)
    buffer = HeadTail(keep)
    assert proc.stdout is not None
    job = BackgroundJob(command, proc, buffer)
    job._threads.append(_pump(proc.stdout, buffer))
    return job
