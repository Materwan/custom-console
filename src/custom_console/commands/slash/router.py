"""What a line typed in the agent's prompt means.

    /name args     a slash command, or one of the shell's own commands (`/ls src`)
    !command       run a command now; its output goes with the next message
    //text, !!text a message that starts with / or !: one character is dropped
    anything else  a message to Clara
"""

from __future__ import annotations

from dataclasses import dataclass

from ...clara import ClaraError
from ...core.agent import AgentError
from ..base import Registry
from .base import SlashContext, SlashRegistry, SlashSpec

BLOCKED_IN_AGENT = frozenset({"cd", "ai", "exit", "reload", "clear"})  # the agent keeps its folder; /clear and /exit are its own


@dataclass(frozen=True, slots=True)
class Routed:
    kind: str  # "empty", "message", "bang", "slash", "shell" or "unknown"
    text: str = ""  # the message, the command line, or the name of the unknown command
    name: str = ""
    args: str = ""


def route(line: str, slash: SlashRegistry, shell: Registry) -> Routed:
    stripped = line.strip()
    if not stripped:
        return Routed("empty")
    if stripped.startswith("//") or stripped.startswith("!!"):
        return Routed("message", line.lstrip()[1:].rstrip())
    if stripped.startswith("!"):
        return Routed("bang", stripped[1:].strip())
    if stripped.startswith("/"):
        name, _, args = stripped[1:].partition(" ")
        args = args.strip()
        if name in slash:
            return Routed("slash", stripped, name, args)
        if name in shell and name not in BLOCKED_IN_AGENT:
            return Routed("shell", f"{name} {args}".strip(), name, args)
        return Routed("unknown", name, name, args)
    return Routed("message", line.rstrip())


def unknown_message(name: str, slash: SlashRegistry, shell: Registry) -> str:
    if name in BLOCKED_IN_AGENT and name in shell:
        return f"/{name} is not available inside the agent console: the agent keeps its folder. /exit leaves, /clear starts over."
    close = [n for n in [*(s.name for s in slash.specs()), *shell.names()] if n.startswith(name[:2])][:5] if name else []
    hint = f" Did you mean {', '.join('/' + n for n in close)}?" if close else ""
    return f"Unknown command /{name}.{hint} /help lists them. To send a message that starts with a slash, start it with //."


async def run_slash(ctx: SlashContext, spec: SlashSpec, args: str) -> bool:
    """Run one slash command. Errors become one line on the screen; returns whether it worked."""
    ctx.name = spec.name
    try:
        await spec.run(ctx, args)
    except AgentError as error:
        ctx.fail(str(error))
    except ClaraError as error:
        ctx.fail(str(error))
    else:
        return True
    return False
