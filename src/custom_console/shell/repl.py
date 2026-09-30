"""The interactive shell loop."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from ..apps.finder import SavedApps, search_path
from ..fs import (
    BinaryFileError,
    FileManager,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    Permissions,
    RemarkableError,
    RemarkableUnavailableError,
    UnsafeOperationError,
)
from ..llm.ollama import OllamaClient, OllamaUnavailableError
from ..settings import Settings, get_settings
from .commands import CommandError, CommandRegistry, HelpRequested, ShellContext, build_registry
from .completion import ShellCompleter
from .printer import Printer
from .tokenizer import TokenizeError, split_command

YES_ANSWERS = ("y", "yes", "o", "oui")


def format_error(command: str, error: BaseException) -> str:
    """One-line, user-facing description of a command failure."""
    detail = str(error)
    if isinstance(error, OSError) and not isinstance(error, RemarkableError):
        # `str(OSError)` embeds the errno text ("[WinError 2] ..."): prefer the
        # offending path, or the bare system message.
        detail = error.filename or error.strerror or detail
    if isinstance(error, CommandError):
        return f"{command}: {detail}"
    if isinstance(error, FileNotFoundError):
        return f"{command}: {detail}: not found"
    if isinstance(error, NotADirectoryError):
        return f"{command}: {detail}: not a directory"
    if isinstance(error, NotAFileError):
        return f"{command}: {detail}: is a directory"
    if isinstance(error, PermissionError):
        return f"{command}: {detail}: permission denied"
    if isinstance(error, OllamaUnavailableError):
        return f"{command}: {detail}"
    if isinstance(
        error,
        (
            IsVirtualRootError,
            InReMarkableError,
            RemarkableUnavailableError,
            RemarkableError,
            UnsafeOperationError,
            BinaryFileError,
            ValueError,
            TimeoutError,
        ),
    ):
        return f"{command}: {detail}"
    return f"{command}: {type(error).__name__}: {detail}"


def ask_yes_no(question: str) -> bool:
    """Ask on the terminal; the default answer is "no"."""
    try:
        answer = prompt(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in YES_ANSWERS


class Shell:
    """Read-eval-print loop over the registered commands."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        registry: Optional[CommandRegistry] = None,
        printer: Optional[Printer] = None,
        files: Optional[FileManager] = None,
        confirm: Callable[[str], bool] = ask_yes_no,
    ) -> None:
        settings = settings or get_settings()
        self.registry = registry or build_registry()
        self.printer = printer or Printer()
        self.files = files or FileManager.from_settings(settings)
        ollama = OllamaClient(settings.ollama_host)
        saved_apps = SavedApps(settings.saved_apps_path)

        self.context = ShellContext(
            settings=settings,
            printer=self.printer,
            files=self.files,
            registry=self.registry,
            ollama=ollama,
            saved_apps=saved_apps,
            confirm=confirm,
            last_model=settings.default_model,
        )
        self.completer = ShellCompleter(
            self.registry,
            self.files,
            saved_apps,
            model_names=lambda: [model.name for model in ollama.installed()],
        )
        self.history = InMemoryHistory()

    # -- execution ---------------------------------------------------------- #

    def execute(self, line: str) -> None:
        """Run one command line. Never raises: errors are printed."""
        try:
            tokens = split_command(line)
        except TokenizeError as error:
            self.printer.error(f"syntax error: {error}")
            return
        if not tokens:
            return

        name, *args = tokens
        command = self.registry.get(name)
        try:
            if command is None:
                self._run_external(name, args)
            else:
                command.handler(self.context, command.parser.parse_args(args))
        except HelpRequested:
            pass
        except KeyboardInterrupt:
            self.printer.warning(f"{name}: interrupted")
        except Exception as error:  # a command must never kill the shell
            self.printer.error(format_error(name, error))

    def _run_external(self, name: str, args: list) -> None:
        program = search_path(name)
        if program is None:
            raise CommandError("command not found")
        cwd = self.files.local_location if not (self.files.in_remarkable or self.files.in_root) else None
        subprocess.run([program, *args], cwd=cwd)

    # -- loop --------------------------------------------------------------- #

    def _print_location(self) -> None:
        if self.files.mode == FileManager.MODE_LOCAL:
            location = self.files.location
            perms = Permissions(
                os.access(location, os.R_OK),
                os.access(location, os.W_OK),
                os.access(location, os.X_OK),
            )
            self.printer.location(location, perms)
        else:
            self.printer.location(self.files.location)

    def run(self) -> None:
        while self.context.running:
            self._print_location()
            try:
                line = prompt(
                    "> ",
                    completer=self.completer,
                    history=self.history,
                    complete_while_typing=False,
                )
            except KeyboardInterrupt:
                self.printer.blank()
                continue
            except EOFError:  # Ctrl+D / Ctrl+Z
                break

            self.execute(line)
            if self.context.running and line.split(None, 1)[:1] != ["clear"]:
                self.printer.blank()
