"""Agent: `ai agent` opens the full-screen agent console (`ai list` and `ai start` are gone: Clara reaches its models itself)."""

from __future__ import annotations

import argparse

from ...core.options import AgentOptions
from ..base import CommandSpec as Spec
from ..base import Context, ShellRequest


def _build(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", default="agent", help="agent")
    p.add_argument("--incognito", action="store_true", help="a conversation Clara forgets: no memory, deleted after 24 hours")
    p.add_argument("--local-only", action="store_true", dest="local_only", help="never use a model that runs off this network")
    p.add_argument("--model", metavar="NAME", help="the model to start with (the list is /model inside the agent)")


def _run(ctx: Context, a: argparse.Namespace) -> None:
    if a.action in ("list", "start"):
        ctx.fail(f"`ai {a.action}` was removed: Clara reaches its models itself. Use `ai agent`, then /model.", 2)
        return
    if a.action != "agent":
        ctx.fail(f"unknown subcommand {a.action!r}: the only one is `ai agent`", 2)
        return
    ctx.shell.request = ShellRequest("agent", AgentOptions(a.incognito, a.local_only, a.model))


COMMANDS = (Spec("ai", "Open the AI agent console (ai agent).", _build, _run, kinds=("none",), group="Agent"),)
