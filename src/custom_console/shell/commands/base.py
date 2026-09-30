"""Command infrastructure: parsers, registry and the shared context."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Callable, Dict, Iterator, List, Optional

if TYPE_CHECKING:
    from ...apps.finder import SavedApps
    from ...fs import FileManager
    from ...llm.ollama import OllamaClient
    from ...settings import Settings
    from ..printer import Printer

# Argument metavars that tell the completer what to suggest.
PATH = "PATH"
APP = "APP"
MODEL = "MODEL"


class CommandError(Exception):
    """A user mistake; the message is shown as ``<command>: <message>``."""


class HelpRequested(Exception):
    """Raised after ``--help`` was printed, to stop the command quietly."""


class ShellParser(argparse.ArgumentParser):
    """argparse parser that never exits the process.

    Usage errors become :class:`CommandError`; ``-h`` prints the help and
    raises :class:`HelpRequested`.
    """

    def error(self, message: str):  # type: ignore[override]
        raise CommandError(message)

    def exit(self, status: int = 0, message: Optional[str] = None):  # type: ignore[override]
        if status:
            raise CommandError((message or "").strip())
        raise HelpRequested()


@dataclass
class ShellContext:
    """Everything a command handler may need."""

    settings: "Settings"
    printer: "Printer"
    files: "FileManager"
    registry: "CommandRegistry"
    ollama: "OllamaClient"
    saved_apps: "SavedApps"
    confirm: Callable[[str], bool]
    last_model: str = ""
    running: bool = True


Handler = Callable[[ShellContext, argparse.Namespace], None]
Configure = Callable[[ShellParser], None]


def _no_arguments(_parser: ShellParser) -> None:
    return None


@dataclass
class Command:
    name: str
    summary: str
    handler: Handler
    configure: Configure = field(default=_no_arguments)

    @cached_property
    def parser(self) -> ShellParser:
        parser = ShellParser(prog=self.name, description=self.summary)
        self.configure(parser)
        return parser


class CommandRegistry:
    def __init__(self, commands: Optional[List[Command]] = None):
        self._commands: Dict[str, Command] = {}
        for command in commands or []:
            self.add(command)

    def add(self, command: Command) -> None:
        self._commands[command.name] = command

    def get(self, name: str) -> Optional[Command]:
        return self._commands.get(name)

    def names(self) -> List[str]:
        return sorted(self._commands)

    def __contains__(self, name: str) -> bool:
        return name in self._commands

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)
