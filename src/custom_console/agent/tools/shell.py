"""`run_command`: let the agent run shell commands (tests, git, pip...), in the foreground or as
background jobs (a dev server, a watcher) whose output it reads later.

Commands run in their own hidden console (they cannot read the keyboard nor write on the agent's
screen), with Python and the console set to UTF-8; what they print is shown live under the tool's
line. Output that is not valid UTF-8 is read in the console's OEM code page.
"""

import base64
import itertools
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Literal, Optional, Tuple

from ..permissions import PermissionLevel, command_rule
from ..results import ToolResult
from .base import ToolContext, guarded

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000
HEAD_CHARS = 6_000  # kept from the start of a long output; the rest comes from the end
JOB_BUFFER_CHARS = 200_000  # what a background job keeps of its output (the end)
MAX_JOBS = 5
MAX_WAIT = 120  # seconds command_output may wait for more output

Shell = Literal["cmd", "powershell"]


def console_encoding() -> str:
    """Encoding of what Windows console programs print when they do not use UTF-8: the OEM code page."""
    if sys.platform == "win32":
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            return "utf-8"
    return "utf-8"


def decode(data: bytes, fallback: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(fallback, errors="replace")


def shorten_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of a long output (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    head = min(HEAD_CHARS, limit // 3)
    tail = limit - head
    skipped = len(text) - limit
    return f"{text[:head]}\n[... {skipped} characters skipped ...]\n{text[-tail:]}"


def kill_tree(process: "subprocess.Popen[bytes]") -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        process.kill()
    except OSError:
        pass


def command_line(command: str, shell: str = "cmd"):
    """What to start for `command` in `shell` (a string for cmd, whose quoting rules are its own)."""
    if sys.platform != "win32":
        return ["/bin/sh", "-c", command]
    if shell == "powershell":
        script = (
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; "
            + command
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    if shell != "cmd":
        raise ValueError(f"Unknown shell {shell!r}: use cmd or powershell.")
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return f'"{comspec}" /d /s /c "chcp 65001 >nul & {command}"'


class Process:
    """A running command and what it printed (stdout and stderr together, in order)."""

    def __init__(self, command: str, cwd: Optional[str], shell: str = "cmd", on_line: Optional[Callable[[str], None]] = None):
        self.command = command
        self.started = time.monotonic()
        self._fallback = console_encoding()
        self._on_line = on_line
        self._lines: List[str] = []
        self._size = 0
        self._dropped = 0  # characters dropped from the start (background jobs keep the end)
        self._read = 0  # lines already handed out by `take_new`
        self._lock = threading.Lock()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.popen = subprocess.Popen(
            command_line(command, shell),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._pump, name="command-output", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.popen.stdout is not None
        for raw in iter(self.popen.stdout.readline, b""):
            line = decode(raw, self._fallback).replace("\r\n", "\n").rstrip("\n")
            with self._lock:
                self._lines.append(line)
                self._size += len(line) + 1
                while self._size > JOB_BUFFER_CHARS and len(self._lines) > 1:
                    dropped = self._lines.pop(0)
                    self._size -= len(dropped) + 1
                    self._dropped += len(dropped) + 1
                    self._read = max(0, self._read - 1)
            if self._on_line is not None:
                try:
                    self._on_line(line)
                except Exception:
                    pass

    @property
    def code(self) -> Optional[int]:
        return self.popen.poll()

    def wait(self, timeout: float) -> bool:
        """True once the command has ended and all its output was read."""
        try:
            self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        self._reader.join(timeout=5)
        return True

    def stop(self) -> None:
        if self.popen.poll() is None:
            kill_tree(self.popen)
        self.wait(5)

    def text(self) -> str:
        with self._lock:
            body = "\n".join(self._lines)
            return f"[... {self._dropped} characters dropped ...]\n{body}" if self._dropped else body

    def take_new(self) -> str:
        """What was printed since the last call."""
        with self._lock:
            new = self._lines[self._read :]
            self._read = len(self._lines)
        return "\n".join(new)


def run_shell(
    command: str,
    cwd: Optional[str],
    timeout: float,
    is_cancelled: Callable[[], bool] = lambda: False,
    *,
    on_line: Optional[Callable[[str], None]] = None,
    shell: str = "cmd",
) -> Tuple[Optional[int], str, str]:
    """Run `command` and wait for it.

    Returns ``(exit code, output, stop reason)``; the exit code is None and the
    reason "timeout" or "cancelled" when the command had to be killed. stdout and
    stderr are merged, in order; `on_line` is told each line as it comes.
    """
    process = Process(command, cwd, shell, on_line)
    deadline = time.monotonic() + timeout
    reason = ""
    while not process.wait(0.2):
        if is_cancelled():
            reason = "cancelled"
        elif time.monotonic() >= deadline:
            reason = "timeout"
        if reason:
            process.stop()
            break
    return (None if reason else process.code), process.text(), reason


def shell_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    files = ctx.files
    jobs: Dict[str, Process] = {}
    numbers = itertools.count(1)

    def stop_all() -> None:
        for job in jobs.values():
            job.stop()
        jobs.clear()

    ctx.on_close(stop_all)

    def working_directory() -> str:
        if files.mode != files.MODE_LOCAL:
            raise ValueError("Move into a local folder first (file_system_cd): commands run in the working directory.")
        return files.local_location

    def describe(command: str, timeout: int = DEFAULT_TIMEOUT, shell: str = "cmd", background: bool = False) -> str:
        how = " in the background" if background else ""
        where = f"{files.location}" + (" (PowerShell)" if shell == "powershell" else "")
        return f"Agent wants to run a command{how} in {where}:\n  {command}"

    def rule(command: str, **_: object) -> Optional[str]:
        return command_rule(command)

    @guarded(ctx, PermissionLevel.WRITE, describe=describe, rule=rule)
    def run_command(
        command: str, timeout: int = DEFAULT_TIMEOUT, shell: Shell = "cmd", background: bool = False
    ) -> ToolResult:
        """Run a shell command in the working directory and return its output (stdout and
        stderr together). The command cannot read the keyboard; give it options that avoid
        prompts. Changes made by commands cannot be undone, so prefer the file_system_*
        tools to change files.

        Args:
            command: the command line.
            timeout: seconds before the command is killed (default 60, at most 600).
            shell: "cmd" (default) or "powershell".
            background: start it and return at once (for servers, watchers, long builds); read
                its output with command_output and stop it with command_stop.
        """
        cwd = working_directory()
        if background:
            running = [key for key, job in jobs.items() if job.code is None]
            if len(running) >= MAX_JOBS:
                raise RuntimeError(f"{MAX_JOBS} background commands already run ({', '.join(running)}): stop one first.")
            job_id = str(next(numbers))
            jobs[job_id] = Process(command, cwd, shell, ctx.progress)
            return ToolResult.ok(
                {"job": job_id, "note": "Started. Read its output with command_output, stop it with command_stop."},
                summary=f"job {job_id}",
            )
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        code, output, reason = run_shell(command, cwd, timeout, ctx.is_cancelled, on_line=ctx.progress, shell=shell)
        output = shorten_output(output.strip("\n"), min(MAX_OUTPUT_CHARS, ctx.max_chars()))
        if reason == "timeout":
            return ToolResult.fail(
                TimeoutError(f"killed after {timeout}s (use background=true for commands that keep running)"),
                {"output": output},
            )
        if reason == "cancelled":
            return ToolResult.fail(RuntimeError("Interrupted by the user."), {"output": output})
        if code != 0:
            return ToolResult.fail(RuntimeError(f"exit code {code}"), {"exit_code": code, "output": output})
        return ToolResult.ok({"exit_code": 0, "output": output})

    def job(job_id: str) -> Process:
        found = jobs.get(str(job_id).strip())
        if found is None:
            known = ", ".join(jobs) or "none"
            raise LookupError(f"No background command {job_id!r} (known: {known}).")
        return found

    @guarded(ctx, PermissionLevel.NONE)
    def command_output(job_id: str, wait_seconds: int = 0) -> ToolResult:
        """What a background command printed since you last asked, and whether it still runs.

        Args:
            job_id: the job number run_command gave.
            wait_seconds: wait up to this many seconds for it to end first (at most 120).
        """
        process = job(job_id)
        deadline = time.monotonic() + max(0, min(int(wait_seconds), MAX_WAIT))
        while not process.wait(0.2) and time.monotonic() < deadline and not ctx.is_cancelled():
            pass
        code = process.code
        status = "running" if code is None else f"ended, exit code {code}"
        new = shorten_output(process.take_new(), min(MAX_OUTPUT_CHARS, ctx.max_chars()))
        return ToolResult.ok({"job": str(job_id), "status": status, "output": new or "(nothing new)"}, summary=status)

    @guarded(ctx, PermissionLevel.NONE)
    def command_stop(job_id: str) -> ToolResult:
        """Stop a background command (and everything it started).

        Args:
            job_id: the job number run_command gave.
        """
        process = job(job_id)
        process.stop()
        tail = shorten_output(process.take_new(), 4_000)
        del jobs[str(job_id).strip()]
        return ToolResult.ok({"job": str(job_id), "status": "stopped", "last_output": tail or "(nothing new)"})

    return [run_command, command_output, command_stop]
