"""The shell's own commands inside the agent console (`/ls src`, `/cat notes.txt | grep todo`).

They run in a worker thread on a copy of the shell that lives in the agent's folder; their output is collected and handed
back to be drawn. Commands that would ask a question on the terminal are refused (the answer is "no"), and so are the ones
that would move the agent or end the program.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field

from ...platform import CancelToken
from ..base import Line, Shell
from ..pipes import run_line


@dataclass(slots=True)
class ShellOutcome:
    lines: list[Line] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: int = 0


def agent_shell(shell: Shell, outcome: ShellOutcome, cancel: CancelToken) -> Shell:
    """A copy of `shell` that collects its output into `outcome`. Its folder is its own (a `cd` does not move the agent)."""
    vfs = shell.vfs.fork(cancel)

    def refuse(question: str) -> bool:
        outcome.errors.append("this asks a question, which the agent console cannot: run it in the shell. (Answered no.)")
        return False

    return dataclasses.replace(shell, vfs=vfs, out=outcome.lines.append, err=outcome.errors.append, confirm=refuse,
                               spinner=lambda label: None, request=None, last_status=0)


async def run_shell_command(shell: Shell, line: str, cancel: CancelToken) -> ShellOutcome:
    outcome = ShellOutcome()
    copy = agent_shell(shell, outcome, cancel)
    outcome.status = await asyncio.to_thread(run_line, copy, line)
    if copy.request is not None:
        outcome.errors.append(f"{copy.request.kind} is not available inside the agent console.")
    return outcome
