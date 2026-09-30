"""`ai` command: list / start Ollama models and run the agent."""

from __future__ import annotations

import argparse
from typing import List

from rich.table import Table

from ...llm.ollama import ModelInfo, format_size
from ..printer import COLOR_ERROR, COLOR_SUCCESS
from .base import MODEL, Command, CommandError, CommandRegistry, ShellContext, ShellParser

# Columns always shown by `ai list -c`, even when no model has the capability.
_BASE_CAPABILITIES = ["completion", "thinking", "vision", "audio", "tools"]


def _ai_args(parser: ShellParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True, parser_class=ShellParser, metavar="{list,start,agent}")

    listing = sub.add_parser("list", help="list the installed models")
    listing.add_argument("-r", "--running", action="store_true", help="only running models")
    listing.add_argument("-s", "--size", action="store_true", help="show sizes")
    listing.add_argument("-c", "--capabilities", action="store_true", help="show capabilities")

    start = sub.add_parser("start", help="load a model in memory")
    start.add_argument("model", nargs="?", metavar=MODEL, help="default: the last used model")

    agent = sub.add_parser("agent", help="open the AI agent console")
    agent.add_argument("-n", "--name", default="My Agent", help="name shown in the agent console")
    agent.add_argument("-m", "--model", metavar=MODEL, help="default: AGENT_DEFAULT_MODEL")
    agent.add_argument(
        "-p",
        "--permissions",
        type=int,
        choices=(0, 1, 2),
        help="auto-accepted tool level: 0=none, 1=reads, 2=everything "
        "(default: AGENT_PERMISSION_LEVEL)",
    )
    agent.add_argument("--no-memory", action="store_true", help="do not keep history/memories")


def _start_model(ctx: ShellContext, model: str) -> None:
    with ctx.printer.status(f"Starting {model}"):
        if ctx.ollama.is_running(model):
            ctx.printer.success(f"ai: {model} already running")
            return
        ctx.ollama.start(model)
    ctx.printer.success(f"{model} started")
    ctx.last_model = model


def _list(ctx: ShellContext, args: argparse.Namespace) -> None:
    models: List[ModelInfo] = ctx.ollama.running() if args.running else ctx.ollama.installed()
    table = Table(title="Running models" if args.running else "Installed models")
    table.add_column("Model", style="green", no_wrap=True)

    if args.size:
        table.add_column("Size", style="blue")

    capabilities = list(_BASE_CAPABILITIES)
    if args.capabilities:
        for model in models:
            capabilities += [c for c in model.capabilities if c not in capabilities]
        for capability in capabilities:
            table.add_column(capability)

    for model in models:
        row = [model.name]
        if args.size:
            row.append(format_size(model.size))
        if args.capabilities:
            row += [
                f"[{COLOR_SUCCESS}]✓[/{COLOR_SUCCESS}]"
                if capability in model.capabilities
                else f"[{COLOR_ERROR}]✗[/{COLOR_ERROR}]"
                for capability in capabilities
            ]
        table.add_row(*row)

    ctx.printer.console.print(table)


def _start(ctx: ShellContext, args: argparse.Namespace) -> None:
    if args.model:
        model = ctx.ollama.find(args.model)
        if not model:
            raise CommandError(f"{args.model} model does not exist")
    else:
        model = ctx.last_model or ctx.settings.default_model
    _start_model(ctx, model)


def _agent(ctx: ShellContext, args: argparse.Namespace) -> None:
    requested = args.model or ctx.settings.default_model
    model = ctx.ollama.find(requested)
    if not model:
        raise CommandError(f"{requested} model does not exist")

    # Cloud models are served remotely: there is nothing to load locally.
    if not model.endswith("cloud") and not ctx.ollama.is_running(model):
        ctx.printer.warning(f"ai: {model} is not running")
        if not ctx.confirm(f"Start {model}?"):
            return
        _start_model(ctx, model)

    from ...agent.console import AgentConsole  # heavy import: only when needed

    level = args.permissions if args.permissions is not None else ctx.settings.agent_permission_level
    AgentConsole(
        settings=ctx.settings,
        model=model,
        name=args.name,
        permission_level=level,
        files=ctx.files.clone(),
        memory=not args.no_memory,
    ).run()
    ctx.last_model = model
    ctx.printer.console.clear()


_ACTIONS = {"list": _list, "start": _start, "agent": _agent}


def _ai(ctx: ShellContext, args: argparse.Namespace) -> None:
    _ACTIONS[args.action](ctx, args)


def register(registry: CommandRegistry) -> None:
    registry.add(Command("ai", "Ollama models and the AI agent (list | start | agent)", _ai, _ai_args))
