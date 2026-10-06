"""Slash commands typed in the agent's input line (``/model``, ``/ls``...).

Handlers run in a worker thread, so they may block (ask the model, list the
reMarkable) and may ask the user a question. What they print is returned as
text and shown above the input line by the screen.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional

from prompt_toolkit.completion import Completer, Completion
from rich.console import Console, RenderableType


@dataclass
class CommandResult:
    text: str = ""  # output, already rendered (may contain ANSI colours)
    prompt: Optional[str] = None  # hand this text to the agent as a new turn
    quit: bool = False  # leave the agent
    clear_screen: bool = False  # wipe the terminal and show the banner again
    transcript: bool = False  # open the full-screen transcript of the conversation


class Renderer:
    """Renders rich objects and messages into strings sized like a real console."""

    def __init__(self, console: Console):
        self.console = console

    def capture(self) -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=self.console.is_terminal,
            color_system=self.console.color_system,
            width=self.console.width,
            legacy_windows=False,
        )

    def render(self, *renderables: RenderableType) -> str:
        target = self.capture()
        for renderable in renderables:
            target.print(renderable)
        return target.file.getvalue()  # type: ignore[union-attr]

    def text(self, message: str, style: Optional[str] = None) -> str:
        """`message` verbatim (no markup interpretation), optionally styled."""
        target = self.capture()
        target.print(message, style=style, markup=False, highlight=False)
        return target.file.getvalue()  # type: ignore[union-attr]


Handler = Callable[[str], CommandResult]
ArgumentCompleter = Callable[[str], Iterable[Completion]]


@dataclass
class SlashCommand:
    name: str
    summary: str
    handler: Handler
    usage: str = ""  # argument synopsis, e.g. "[MODEL]"; empty when the command takes none
    complete: Optional[ArgumentCompleter] = None
    group: str = "Agent"


class SlashRegistry:
    def __init__(self, renderer: Renderer):
        self.renderer = renderer
        self._commands: Dict[str, SlashCommand] = {}
        self._aliases: Dict[str, str] = {}

    def add(self, command: SlashCommand, *aliases: str) -> None:
        self._commands[command.name] = command
        for alias in aliases:
            self._aliases[alias] = command.name

    def get(self, name: str) -> Optional[SlashCommand]:
        name = name.lower()
        return self._commands.get(self._aliases.get(name, name))

    def commands(self) -> List[SlashCommand]:
        """Every command once (aliases excluded), in registration order."""
        return list(self._commands.values())

    def run(self, line: str) -> CommandResult:
        """Execute ``/name arguments``. Unknown commands are reported, not raised."""
        name, _, arguments = line.strip()[1:].partition(" ")
        command = self.get(name)
        if command is None:
            return CommandResult(text=self.renderer.text(f"Unknown command: /{name} (try /help)", style="red"))
        return command.handler(arguments.strip())


class SlashCompleter(Completer):
    """Completes ``/command`` names, then the arguments of the chosen command."""

    def __init__(self, registry: SlashRegistry):
        self.registry = registry

    def get_completions(self, document, complete_event) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        if " " not in body:
            for command in self.registry.commands():
                if command.name.startswith(body.lower()):
                    yield Completion(
                        f"/{command.name}" + (" " if command.usage else ""),
                        start_position=-len(text),
                        display=f"/{command.name}",
                        display_meta=command.summary,
                    )
            return
        name, _, arguments = body.partition(" ")
        command = self.registry.get(name)
        if command is not None and command.complete is not None:
            yield from command.complete(arguments)
