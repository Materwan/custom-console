"""Shell commands that are not about files: echo, clear, exit, help, launch, reload."""

from __future__ import annotations

import argparse

from ...apps.finder import MAX_LEVEL, find_application
from ...apps.launcher import launch, relaunch_console
from .base import APP, Command, CommandError, CommandRegistry, ShellContext, ShellParser


def _echo_args(parser: ShellParser) -> None:
    parser.add_argument("text", nargs="*")


def _echo(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.printer.plain(" ".join(args.text))


def _clear(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.printer.console.clear()


def _exit(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.running = False


def _help_args(parser: ShellParser) -> None:
    parser.add_argument("command", nargs="?", help="show the usage of this command")


def _help(ctx: ShellContext, args: argparse.Namespace) -> None:
    if args.command:
        command = ctx.registry.get(args.command)
        if command is None:
            raise CommandError(f"unknown command '{args.command}'")
        ctx.printer.text(command.parser.format_help())
        return

    width = max(len(name) for name in ctx.registry.names())
    for name in ctx.registry.names():
        ctx.printer.plain(f"{name.ljust(width)}  {ctx.registry.get(name).summary}")
    ctx.printer.plain("\nType `help <command>` (or `<command> -h`) for details.", "dim")


def _launch_args(parser: ShellParser) -> None:
    parser.add_argument("app", metavar=APP, help="application name, or a path with --file")
    parser.add_argument("-f", "--file", action="store_true", help="`app` is a path to start as is")
    parser.add_argument(
        "-sl",
        "--search-level",
        "--search_level",
        dest="search_level",
        type=int,
        default=MAX_LEVEL,
        help="search stages to run: 1=PATH, 2=+registry, 3=+install folders, "
        "4=+recursive search (default), -1=all",
    )


def _launch(ctx: ShellContext, args: argparse.Namespace) -> None:
    if args.file:
        path = args.app
    else:
        if args.search_level != -1 and not 1 <= args.search_level <= MAX_LEVEL:
            raise CommandError(f"search level must be between 1 and {MAX_LEVEL} (or -1)")
        with ctx.printer.status("Searching...") as status:
            path = find_application(
                args.app,
                level=args.search_level,
                saved=ctx.saved_apps,
                on_stage=lambda label: status.update(f"Searching in {label}..."),
            )
        if not path:
            raise CommandError("executable not found")

    ctx.printer.info(path)
    launch(path)


def _reload(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.printer.info("Reloading...")
    relaunch_console()
    ctx.running = False


def register(registry: CommandRegistry) -> None:
    registry.add(Command("echo", "Print text", _echo, _echo_args))
    registry.add(Command("clear", "Clear the screen", _clear))
    registry.add(Command("exit", "Leave the console", _exit))
    registry.add(Command("help", "List commands", _help, _help_args))
    registry.add(Command("launch", "Start an application by name", _launch, _launch_args))
    registry.add(Command("reload", "Restart the console in a new window", _reload))
