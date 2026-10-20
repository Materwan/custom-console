"""A program under a pseudo-terminal that a test types at and waits on (POSIX only)."""

from __future__ import annotations

import os
import pty
import select
import signal
import sys
import time
from pathlib import Path


class Pty:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path):
        self.output = b""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child
            os.chdir(cwd)
            os.execvpe(sys.executable, [sys.executable, "-B", *argv], env)
        self.status: int | None = None

    def read(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.output += chunk

    def send(self, data: bytes | str) -> None:
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def expect(self, text: str, timeout: float = 15.0, after: int = 0) -> None:
        """Wait until `text` appears in what was printed after position `after`; fails with the tail of the output."""
        end = time.time() + timeout
        needle = text.encode()
        while time.time() < end:
            if needle in self.output[after:]:
                return
            self.read(0.1)
        raise AssertionError(f"did not see {text!r} in {timeout:.0f} s; the output ended with:\n"
                             f"{self.output[-1500:].decode('utf-8', 'replace')}")

    def mark(self) -> int:
        return len(self.output)

    def wait_exit(self, timeout: float = 10.0) -> int:
        end = time.time() + timeout
        while time.time() < end:
            self.read(0.1)
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.status = os.waitstatus_to_exitcode(status)
                return self.status
        self.kill()
        raise AssertionError("the program did not exit")

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    @property
    def text(self) -> str:
        return self.output.decode("utf-8", "replace")
