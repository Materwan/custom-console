"""What every shell command shares.

A command is one function: it receives a `Context` and its parsed arguments and returns *lines* (data, not printed text),
so a pipe can carry them to the next command. Errors go to `ctx.fail`, which prints `name: message` on the error stream
and sets the exit status; they are not part of the lines.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.options import AgentOptions
from ..core.vfs.guard import OverwriteGuard
from ..core.vfs.ops import Vfs
from ..paths import DataLayout
from ..platform import CancelToken, Platform


@dataclass(frozen=True, slots=True)
class Line:
    """One line of output: pieces of text, each with a Rich style (empty: none). A pipe only carries `plain`."""

    segments: tuple[tuple[str, str], ...] = ()

    @property
    def plain(self) -> str:
        return "".join(text for text, _ in self.segments)

    @staticmethod
    def of(text: str, style: str = "") -> Line:
        return Line(((text, style),))

    @staticmethod
    def join(*pieces: tuple[str, str]) -> Line:
        return Line(tuple(pieces))


def text_line(text: str, style: str = "") -> Line:
    return Line.of(text, style)


class CommandError(Exception):
    """A command could not do what was asked; the message is the whole explanation."""


class UsageError(CommandError):
    """The command line is wrong (bad option, missing argument)."""


class ParseError(CommandError):
    """The line cannot be understood (an unclosed quote, an unsupported operator)."""


@dataclass(slots=True)
class ShellRequest:
    """Something a command asks the shell loop to do after it returns."""

    kind: str  # "exit", "clear", "reload" or "agent"
    agent: AgentOptions | None = None


@dataclass
class Shell:
    """The state the commands share: the file system, the saved things, and the way out to the screen."""

    vfs: Vfs
    platform: Platform
    layout: DataLayout
    guard: OverwriteGuard
    aliases: Any  # commands.aliases.Aliases
    history: Any  # commands.history.History
    apps: Any  # commands.shell.apps.AppCache
    registry: Registry
    env: Mapping[str, str] = field(default_factory=dict)
    remarkable: Any = None  # RemarkableBackend | None
    out: Callable[[Line], None] = lambda line: None
    err: Callable[[str], None] = lambda message: None
    confirm: Callable[[str], bool] = lambda question: False
    spinner: Callable[[str | None], None] = lambda label: None
    width: Callable[[], int] = lambda: 80
    unicode: bool = True
    request: ShellRequest | None = None
    previous_dir: str | None = None
    last_status: int = 0

    @property
    def cancel(self) -> CancelToken:
        return self.vfs.cancel


@dataclass
class Context:
    shell: Shell
    name: str
    stdin: Iterator[Line] | None = None
    piped_out: bool = False  # the output goes to a pipe or a file, not to the screen
    status: int = 0

    @property
    def vfs(self) -> Vfs:
        return self.shell.vfs

    def fail(self, message: str, status: int = 1) -> None:
        self.shell.err(f"{self.name}: {message}")
        self.status = max(self.status, status)

    def stdin_lines(self) -> Iterator[Line]:
        return self.stdin if self.stdin is not None else iter(())

    @property
    def has_stdin(self) -> bool:
        return self.stdin is not None


class QuietParser(argparse.ArgumentParser):
    """argparse that raises instead of printing and exiting."""

    def error(self, message: str) -> Any:
        raise UsageError(f"{message}\n{self.format_usage().strip()}")

    def exit(self, status: int = 0, message: str | None = None) -> Any:
        raise UsageError(message.strip() if message else "")


Runner = Callable[["Context", argparse.Namespace], "Iterable[Line] | None"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    build: Callable[[argparse.ArgumentParser], None]
    run: Runner
    kinds: tuple[str, ...] = ("any",)  # what each positional argument is: any, file, dir, none, app, command, alias
    rest: str | None = None  # the kind of positionals beyond `kinds` (default: the last of `kinds`)
    globs: bool = False  # expand `*`, `?`, `[a-z]` and `{a,b}` in unquoted arguments
    glob_skip: int = 0  # leading positional arguments that are not paths (grep's pattern)
    group: str = "Shell"

    def parser(self) -> argparse.ArgumentParser:
        parser = QuietParser(prog=self.name, description=self.summary, add_help=False)
        parser.add_argument("-h", "--help", action="store_true", dest="help", help="show this help")
        self.build(parser)
        return parser

    def kind_at(self, index: int) -> str:
        if index < len(self.kinds):
            return self.kinds[index]
        return self.rest if self.rest is not None else self.kinds[-1]


class Registry:
    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}

    def add(self, spec: CommandSpec) -> None:
        self._commands[spec.name] = spec

    def get(self, name: str) -> CommandSpec | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def specs(self) -> list[CommandSpec]:
        return [self._commands[n] for n in self.names()]

    def __contains__(self, name: object) -> bool:
        return name in self._commands
