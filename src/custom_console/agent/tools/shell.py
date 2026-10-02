"""`run_command`: let the agent run shell commands (tests, git, pip...)."""

import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000
HEAD_CHARS = 6_000  # kept from the start of a long output; the rest comes from the end


def console_encoding() -> str:
    """Encoding of what commands print: the OEM code page on Windows."""
    if sys.platform == "win32":
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            return "utf-8"
    return "utf-8"


def shorten_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of a long output (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    tail = limit - HEAD_CHARS
    skipped = len(text) - limit
    return f"{text[:HEAD_CHARS]}\n[... {skipped} characters skipped ...]\n{text[-tail:]}"


def kill_tree(process: "subprocess.Popen[bytes]") -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    process.kill()


def run_shell(
    command: str,
    cwd: Optional[str],
    timeout: float,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Tuple[Optional[int], str, str]:
    """Run `command` through the system shell.

    Returns ``(exit code, output, stop reason)``; the exit code is None and the
    reason "timeout" or "cancelled" when the command had to be killed. stdout and
    stderr are merged, in order.
    """
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + timeout
    chunks: List[bytes] = []
    reason = ""
    while True:
        try:
            out, _ = process.communicate(timeout=0.2)
            chunks.append(out or b"")
            break
        except subprocess.TimeoutExpired:
            if is_cancelled():
                reason = "cancelled"
            elif time.monotonic() >= deadline:
                reason = "timeout"
            if reason:
                kill_tree(process)
                out, _ = process.communicate()
                chunks.append(out or b"")
                break
    text = b"".join(chunks).decode(console_encoding(), errors="replace").replace("\r\n", "\n")
    return (None if reason else process.returncode), text, reason


def shell_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    files = ctx.files

    def working_directory() -> str:
        if files.mode != files.MODE_LOCAL:
            raise ValueError("Move into a local folder first (file_system_cd): commands run in the working directory.")
        return files.local_location

    def describe(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        return f"Agent wants to run a command in {files.location}:\n  {command}"

    @guarded(ctx, PermissionLevel.WRITE, describe=describe)
    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
        """Run a shell command in the working directory and return its output (stdout and
        stderr together). On Windows it runs through cmd.exe. The command cannot read the
        keyboard; give it options that avoid prompts. Changes made by commands cannot be
        undone, so prefer the file_system_* tools to change files.

        Args:
            command: the command line.
            timeout: seconds before the command is killed (default 60, at most 600).
        """
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        code, output, reason = run_shell(command, working_directory(), timeout, ctx.is_cancelled)
        output = shorten_output(output.strip("\n"))
        if reason == "timeout":
            return ToolResult.fail(TimeoutError(f"killed after {timeout}s"), {"output": output})
        if reason == "cancelled":
            return ToolResult.fail(RuntimeError("Interrupted by the user."), {"output": output})
        if code != 0:
            return ToolResult.fail(RuntimeError(f"exit code {code}"), {"exit_code": code, "output": output})
        return ToolResult.ok({"exit_code": 0, "output": output})

    return [run_command]
