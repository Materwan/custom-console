"""Shell: `echo`, `clear`, `exit`, `help`, `reload`, `alias`, `unalias`, `history`, `which`, `env`."""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Iterator

from ..base import CommandSpec as Spec
from ..base import Context, Line, ShellRequest
from ..pipes import environment_lines
from .helpers import DIM, HEADING


def _echo_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-n", dest="no_newline", action="store_true", help="accepted for habit; each echo is one line")
    p.add_argument("words", nargs="*")


def _echo_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    yield Line.of(" ".join(a.words))


def _none(p: argparse.ArgumentParser) -> None:
    return None


def _clear_run(ctx: Context, a: argparse.Namespace) -> None:
    ctx.shell.request = ShellRequest("clear")


def _exit_run(ctx: Context, a: argparse.Namespace) -> None:
    ctx.shell.request = ShellRequest("exit")


def _reload_run(ctx: Context, a: argparse.Namespace) -> None:
    ctx.shell.request = ShellRequest("reload")  # the shell loop restarts after leaving its prompt cleanly


# ---------------------------------------------------------------------- help
def _help_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("command", nargs="?", help="a command to explain")


def _help_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    registry = ctx.shell.registry
    if a.command:
        spec = registry.get(a.command)
        if spec is None:
            ctx.fail(f"no such command: {a.command}")
            return
        for text in spec.parser().format_help().rstrip().split("\n"):
            yield Line.of(text)
        return
    groups: dict[str, list[Spec]] = {}
    for spec in registry.specs():
        groups.setdefault(spec.group, []).append(spec)
    order = ["Navigate", "Read", "Change", "Shell", "Desktop", "reMarkable", "Agent"]
    for name in sorted(groups, key=lambda g: order.index(g) if g in order else len(order)):
        yield Line.of(name, HEADING)
        for spec in groups[name]:
            yield Line.join((f"  {spec.name:<10}", "bold"), (spec.summary, ""))
        yield Line.of("")
    yield Line.of("Pipes (|) and redirection (<, >, >>, >|) work between built-in commands. Tab completes; <command> -h explains one.", DIM)


# ---------------------------------------------------------------------- alias, unalias
def _alias_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("definitions", nargs="*", help="name=value to define, or a name to show (none: list them all)")


def _alias_line(name: str, value: str) -> Line:
    return Line.of(f"alias {name}='{value.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")


def _alias_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    aliases = ctx.shell.aliases
    if not a.definitions:
        for name, value in aliases.all().items():
            yield _alias_line(name, value)
        return
    for item in a.definitions:
        name, equals, value = item.partition("=")
        if not equals:
            current = aliases.get(name)
            if current is None:
                ctx.fail(f"{name}: not found")
            else:
                yield _alias_line(name, current)
            continue
        try:
            aliases.set(name, value)
        except ValueError as error:
            ctx.fail(str(error))


def _unalias_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-a", "--all", action="store_true", help="remove every alias")
    p.add_argument("names", nargs="*")


def _unalias_run(ctx: Context, a: argparse.Namespace) -> None:
    if a.all:
        ctx.shell.aliases.clear()
        return
    if not a.names:
        ctx.fail("give an alias name, or -a", 2)
    for name in a.names:
        if not ctx.shell.aliases.remove(name):
            ctx.fail(f"{name}: not found")


# ---------------------------------------------------------------------- history, which, env
def _history_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--clear", action="store_true", help="forget the saved history")
    p.add_argument("count", nargs="?", type=int, help="show only the last N lines")


def _history_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    history = ctx.shell.history
    if a.clear:
        history.clear()
        return
    entries = history.entries
    first = max(len(entries) - a.count, 0) if a.count else 0
    for number, line in enumerate(entries[first:], first + 1):
        yield Line.join((f"{number:>5}  ", DIM), (line, ""))


def _which_build(p: argparse.ArgumentParser) -> None:
    p.add_argument("names", nargs="+", help="what to look for")


def _which_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    shell = ctx.shell
    for name in a.names:
        alias = shell.aliases.get(name)
        if alias is not None:
            yield Line.of(f"{name}: aliased to '{alias}'")
        if name in shell.registry:
            yield Line.of(f"{name}: shell built-in")
        elif alias is None:
            found = shutil.which(name, path=(shell.env or os.environ).get("PATH"))
            if found:
                yield Line.of(f"{name}: {found}")
            else:
                ctx.fail(f"no {name} in the PATH", 1)


def _env_run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    for text in environment_lines(dict(ctx.shell.env or os.environ)):
        yield Line.of(text)


COMMANDS = (
    Spec("echo", "Print the words.", _echo_build, _echo_run, kinds=("none",), group="Shell"),
    Spec("clear", "Clear the screen.", _none, _clear_run, kinds=("none",), group="Shell"),
    Spec("exit", "Leave the console.", _none, _exit_run, kinds=("none",), group="Shell"),
    Spec("help", "List the commands, or explain one.", _help_build, _help_run, kinds=("command",), group="Shell"),
    Spec("reload", "Start a fresh copy of the console and leave this one.", _none, _reload_run, kinds=("none",), group="Shell"),
    Spec("alias", "Define a short name for a command, or list them.", _alias_build, _alias_run, kinds=("alias",), group="Shell"),
    Spec("unalias", "Remove aliases.", _unalias_build, _unalias_run, kinds=("alias",), group="Shell"),
    Spec("history", "Show the saved command history.", _history_build, _history_run, kinds=("none",), group="Shell"),
    Spec("which", "Say whether a name is a built-in, an alias or a program, and where it is.", _which_build, _which_run,
         kinds=("command",), group="Shell"),
    Spec("env", "Show the environment, with secret-looking values hidden.", _none, _env_run, kinds=("none",), group="Shell"),
)

