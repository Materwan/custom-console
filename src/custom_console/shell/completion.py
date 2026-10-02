"""Tab completion, derived from the argparse definition of each command."""

from __future__ import annotations

import argparse
import os
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from prompt_toolkit.completion import Completer, Completion

from ..apps.finder import SavedApps
from ..fs import FileManager
from .commands import APP, MODEL, PATH, PROVIDER, CommandRegistry
from .tokenizer import TokenizeError, current_word, quote_if_needed, split_command, unquote_word

Suggestion = Tuple[str, str]  # (text to insert, text to display)


def list_path_executables() -> Set[str]:
    """Names of the executables found in the PATH (without their extension)."""
    executables: Set[str] = set()
    extensions = {
        ext.upper() for ext in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep)
    }
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in os.listdir(directory):
                name, ext = os.path.splitext(entry)
                if ext.upper() in extensions:
                    executables.add(name)
                elif not ext:
                    executables.add(entry)
        except OSError:
            continue
    return executables


class ArgumentSpec:
    """What the completer needs to know about one parser."""

    def __init__(self, parser: argparse.ArgumentParser):
        self.flags: Dict[str, argparse.Action] = {}
        self.positionals: List[argparse.Action] = []
        self.subcommands: Optional[Dict[str, argparse.ArgumentParser]] = None

        for action in parser._actions:  # argparse exposes no public API for this
            if isinstance(action, argparse._HelpAction):
                continue
            if isinstance(action, argparse._SubParsersAction):
                self.subcommands = dict(action.choices)
            elif action.option_strings:
                for name in action.option_strings:
                    self.flags[name] = action
            else:
                self.positionals.append(action)

    @staticmethod
    def takes_value(action: argparse.Action) -> bool:
        return action.nargs != 0

    def positional_at(self, index: int) -> Optional[argparse.Action]:
        """The positional argument receiving the `index`-th free token."""
        for action in self.positionals:
            if action.nargs in ("*", "+"):
                return action
            width = action.nargs if isinstance(action.nargs, int) else 1
            if index < width:
                return action
            index -= width
        return None


class ShellCompleter(Completer):
    def __init__(
        self,
        registry: CommandRegistry,
        files: FileManager,
        saved_apps: Optional[SavedApps] = None,
        model_names: Optional[Callable[[], Iterable[str]]] = None,
        executables: Optional[Callable[[], Set[str]]] = None,
    ):
        self.registry = registry
        self.files = files
        self.saved_apps = saved_apps
        self.model_names = model_names
        self._executables_loader = executables or list_path_executables
        self._executables: Optional[Set[str]] = None

    # -- entry point -------------------------------------------------------- #

    def get_completions(self, document, complete_event) -> Iterator[Completion]:
        text = document.text_before_cursor
        word = current_word(text)
        try:
            tokens = split_command(text[: len(text) - len(word)])
        except TokenizeError:
            return

        suggestions = (
            self._command_names(unquote_word(word))
            if not tokens
            else self._arguments(tokens, unquote_word(word))
        )
        for insert, display in suggestions:
            yield Completion(insert, start_position=-len(word), display=display)

    # -- command names ------------------------------------------------------ #

    def _command_names(self, prefix: str) -> Iterator[Suggestion]:
        seen: Set[str] = set()
        for name in self.registry.names():
            if name.startswith(prefix):
                seen.add(name)
                yield name, name
        if self._executables is None:
            self._executables = self._executables_loader()
        for name in sorted(self._executables):
            if name.startswith(prefix) and name not in seen:
                yield name, name

    # -- arguments ---------------------------------------------------------- #

    def _arguments(self, tokens: List[str], prefix: str) -> Iterator[Suggestion]:
        command = self.registry.get(tokens[0])
        if command is None:
            return
        yield from self._complete(ArgumentSpec(command.parser), tokens[1:], prefix)

    def _complete(self, spec: ArgumentSpec, tokens: List[str], prefix: str) -> Iterator[Suggestion]:
        free_tokens = 0
        pending_flag: Optional[argparse.Action] = None

        for index, token in enumerate(tokens):
            if pending_flag is not None:
                pending_flag = None
                continue
            flag = spec.flags.get(token)
            if flag is not None:
                pending_flag = flag if spec.takes_value(flag) else None
            elif token.startswith("-") and len(token) > 1:
                continue  # unknown flag: ignore
            elif spec.subcommands is not None and free_tokens == 0 and token in spec.subcommands:
                yield from self._complete(
                    ArgumentSpec(spec.subcommands[token]), tokens[index + 1 :], prefix
                )
                return
            else:
                free_tokens += 1

        if pending_flag is not None:
            yield from self._values(pending_flag.metavar, prefix)
            return

        if prefix.startswith("-"):
            for name in sorted(spec.flags):
                if name.startswith(prefix):
                    yield name, name
            return

        if spec.subcommands is not None and free_tokens == 0:
            for name in spec.subcommands:
                if name.startswith(prefix):
                    yield name, name
            return

        action = spec.positional_at(free_tokens)
        if action is not None:
            yield from self._values(action.metavar, prefix)

    # -- values ------------------------------------------------------------- #

    def _values(self, kind: object, prefix: str) -> Iterator[Suggestion]:
        if kind == PATH:
            for text, is_dir in self.files.suggest(prefix):
                inserted = quote_if_needed(text + ("/" if is_dir else ""))
                yield inserted, text.rsplit("/", 1)[-1] + ("/" if is_dir else "")
        elif kind == PROVIDER:
            from ..llm.providers import PROVIDERS

            for name in PROVIDERS:
                if name.startswith(prefix.lower()):
                    yield name, name
        elif kind == APP and self.saved_apps is not None:
            for name in self.saved_apps.names():
                if name.startswith(prefix.lower()):
                    yield name, name
        elif kind == MODEL and self.model_names is not None:
            try:
                names = list(self.model_names())
            except Exception:  # Ollama not running: nothing to suggest
                return
            for name in names:
                if name.lower().startswith(prefix.lower()):
                    yield name, name
