"""`ai` command: list / start models and run the agent, with the local Ollama,
Ollama's API (ollama.com) or ChatGPT (the OpenAI API)."""

from __future__ import annotations

import argparse
from typing import List

from rich.table import Table

from ...llm.errors import ProviderUnavailableError
from ...llm.keys import KeyStore
from ...llm.ollama import ModelInfo, format_size
from ...llm.providers import (
    Connection,
    Provider,
    ProviderMemory,
    connect,
    find_model,
    get_provider,
    initial_provider,
    preferred_model,
)
from ..printer import COLOR_ERROR, COLOR_SUCCESS
from .base import MODEL, PATH, PROVIDER, Command, CommandError, CommandRegistry, ShellContext, ShellParser

PROVIDER_HELP = "ollama (this computer), ollama-cloud (ollama.com, API key) or chatgpt (OpenAI API, API key)"

# Columns always shown by `ai list -c`, even when no model has the capability.
_BASE_CAPABILITIES = ["completion", "thinking", "vision", "audio", "tools"]


def _ai_args(parser: ShellParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True, parser_class=ShellParser, metavar="{list,start,agent}")

    listing = sub.add_parser("list", help="list the models (default: of the local Ollama)")
    listing.add_argument("-P", "--provider", metavar=PROVIDER, help=PROVIDER_HELP)
    listing.add_argument("-r", "--running", action="store_true", help="only running models (local Ollama)")
    listing.add_argument("-s", "--size", action="store_true", help="show sizes")
    listing.add_argument("-c", "--capabilities", action="store_true", help="show capabilities")

    start = sub.add_parser("start", help="load a model in memory")
    start.add_argument("model", nargs="?", metavar=MODEL, help="default: the last used model")

    agent = sub.add_parser("agent", help="open the AI agent console")
    agent.add_argument("-n", "--name", default="My Agent", help="name shown in the agent console")
    agent.add_argument(
        "-P", "--provider", metavar=PROVIDER, help=f"{PROVIDER_HELP}; default: the last one used, else AGENT_PROVIDER"
    )
    agent.add_argument(
        "-m", "--model", metavar=MODEL, help="default: the last one used with the provider, else its default model"
    )
    agent.add_argument(
        "-p",
        "--permissions",
        type=int,
        choices=(0, 1, 2),
        help="auto-accepted tool level: 0=none, 1=reads, 2=everything "
        "(default: AGENT_PERMISSION_LEVEL)",
    )
    agent.add_argument("--no-memory", action="store_true", help="do not keep history/memories")
    agent.add_argument(
        "-d",
        "--dir",
        metavar=PATH,
        help="folder where file tools need no permission (default: the current directory)",
    )


def _start_model(ctx: ShellContext, model: str) -> None:
    with ctx.printer.status(f"Starting {model}"):
        if ctx.ollama.is_running(model):
            ctx.printer.success(f"ai: {model} already running")
            return
        ctx.ollama.start(model)
    ctx.printer.success(f"{model} started")
    ctx.last_model = model


def _ask_secret(prompt_text: str) -> str:
    from prompt_toolkit import prompt

    try:
        return prompt(f"{prompt_text}\nkey > ", is_password=True)
    except (EOFError, KeyboardInterrupt):
        return ""


def _provider(name: str) -> Provider:
    try:
        return get_provider(name)
    except LookupError as error:
        raise CommandError(str(error)) from None


def _connect(ctx: ShellContext, provider: Provider) -> Connection:
    """The provider's key (asked once, then saved) and its models."""
    try:
        connection = connect(
            provider,
            ctx.keys or KeyStore(),
            ctx.ask_secret or _ask_secret,
            lambda p, key: ctx.ollama if p.local else p.catalog(ctx.settings, key),
        )
    except ProviderUnavailableError as error:
        raise CommandError(f"{provider.label}: {error}") from None
    if connection is None:
        raise CommandError(f"{provider.label} needs an API key")
    for note in connection.notes:
        ctx.printer.success(note)
    return connection


def _list(ctx: ShellContext, args: argparse.Namespace) -> None:
    provider = _provider(args.provider or "ollama")
    if provider.local:
        models: List[ModelInfo] = ctx.ollama.running() if args.running else ctx.ollama.installed()
        title = "Running models" if args.running else "Installed models"
    else:
        if args.running:
            raise CommandError(f"--running is for the local Ollama: {provider.label} runs its models itself")
        models = _connect(ctx, provider).models
        title = f"Models of {provider.label}"
    table = Table(title=title)
    table.add_column("Model", style="green", no_wrap=True)
    if not provider.local:
        table.add_column("Context", justify="right")

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
        if not provider.local:
            row.append(f"{model.context_length:,}" if model.context_length else "?")
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
    memory = ProviderMemory(ctx.settings.agent_provider_path)
    provider = _provider(args.provider) if args.provider else initial_provider(ctx.settings, memory)
    requested = args.model or preferred_model(provider, ctx.settings, memory)
    api_key = None

    if provider.local:
        model = ctx.ollama.find(requested)
        if not model:
            raise CommandError(f"{requested} model does not exist")
        # Cloud models are served remotely: there is nothing to load locally.
        if not model.endswith("cloud") and not ctx.ollama.is_running(model):
            ctx.printer.warning(f"ai: {model} is not running")
            if not ctx.confirm(f"Start {model}?"):
                return
            _start_model(ctx, model)
    else:
        connection = _connect(ctx, provider)
        info = find_model(connection.models, requested) or connection.catalog.info(requested)
        if info is None:
            raise CommandError(
                f"{requested} is not available from {provider.label} (see `ai list -P {provider.name}`)"
            )
        model, api_key = info.name, connection.key

    from ...agent.console import AgentConsole  # heavy import: only when needed

    level = args.permissions if args.permissions is not None else ctx.settings.agent_permission_level
    files = ctx.files.clone()
    if args.dir:
        files.change_directory(args.dir)
    console = AgentConsole(
        settings=ctx.settings,
        model=model,
        name=args.name,
        permission_level=level,
        files=files,
        memory=not args.no_memory,
        ollama=ctx.ollama,
        provider=provider.name,
        api_key=api_key,
        keys=ctx.keys,
    )
    console.run()
    if console.provider.local:
        ctx.last_model = console.model  # /model may have changed it (it is what `ai start` loads)
    ctx.printer.console.clear()


_ACTIONS = {"list": _list, "start": _start, "agent": _agent}


def _ai(ctx: ShellContext, args: argparse.Namespace) -> None:
    _ACTIONS[args.action](ctx, args)


def register(registry: CommandRegistry) -> None:
    registry.add(Command("ai", "models (Ollama, ollama.com, ChatGPT) and the AI agent (list | start | agent)", _ai, _ai_args))
