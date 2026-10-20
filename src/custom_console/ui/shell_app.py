"""The stand-alone shell: a prompt, the output in the normal terminal scrollback, and `ai agent` one command away.

The prompt is prompt_toolkit's, not Textual's: Textual has no inline mode on Windows (see docs/SPIKES.md). Everything that
decides anything lives below this module; this one only draws: it asks the completion engine for candidates, hands each
typed line to the command layer, and prints the lines that come back. Textual draws the full-screen agent, between two prompts
of the same process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import History as PromptHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.text import Text

from ..commands.base import Line, Shell
from ..commands.completion.engine import CompletionEngine
from ..commands.pipes import run_line
from ..commands.shell import new_shell
from ..core.options import AgentOptions
from ..core.runtime import Runtime
from ..core.vfs.ops import Vfs
from ..core.vfs.remarkable import RemarkableBackend, RmapiRunner, find_rmapi
from ..core.vfs.resolver import TABLET, Resolver
from ..platform import Cancelled, CancelToken, DesktopError

log = logging.getLogger("custom_console.shell")
TEMP_AGE = 24 * 3600

STYLE = Style.from_dict({
    "location": "ansiyellow", "granted": "ansigreen", "denied": "ansired", "prompt": "bold",
    "bottom-toolbar": "noreverse ansibrightblack", "completion-menu.completion": "bg:ansibrightblack ansiwhite",
    "completion-menu.completion.current": "bg:ansiblue ansiwhite", "completion-menu.meta.completion": "bg:ansibrightblack ansiyellow",
    "scrollbar.background": "bg:ansibrightblack", "scrollbar.button": "bg:ansiwhite",
})


# ---------------------------------------------------------------------- the pieces of the prompt
class ToolbarState:
    """What the line under the prompt says ('loading...', a slow tablet's message)."""

    def __init__(self) -> None:
        self.text = ""

    def set(self, text: str) -> None:
        self.text = text
        with contextlib.suppress(Exception):
            get_app().invalidate()


class ShellCompleter(Completer):
    """Asks the completion engine in a worker thread. A new keystroke cancels the old request, and the thread with it."""

    def __init__(self, engine: CompletionEngine, toolbar: ToolbarState):
        self.engine = engine
        self.toolbar = toolbar

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        return iter(())  # only the asynchronous form is used

    async def get_completions_async(self, document: Document, complete_event: CompleteEvent) -> Any:
        text, cursor = document.text, document.cursor_position
        token = CancelToken()
        loop = asyncio.get_running_loop()
        self.toolbar.set("loading...")
        try:
            result = await loop.run_in_executor(None, self.engine.complete, text, cursor, token)
        except asyncio.CancelledError:
            token.cancel()
            raise
        except Cancelled:
            return
        finally:
            self.toolbar.set("")
        if result.message:
            self.toolbar.set(result.message)
        for candidate in result.candidates:
            yield Completion(candidate.text, start_position=result.start - cursor, display=candidate.display,
                             display_meta=candidate.meta)


class HistorySuggester(AutoSuggest):
    """Ghost text: the rest of the newest history line that starts with what is typed."""

    def __init__(self, engine: CompletionEngine):
        self.engine = engine

    def get_suggestion(self, buffer: Any, document: Document) -> Suggestion | None:
        if document.cursor_position != len(document.text):
            return None
        rest = self.engine.ghost(document.text)
        return Suggestion(rest) if rest else None


class SavedHistory(PromptHistory):
    """prompt_toolkit's history, kept in the console's own capped history file (secret-looking lines are not saved)."""

    def __init__(self, shell: Shell):
        super().__init__()
        self.shell = shell

    def load_history_strings(self) -> Iterable[str]:
        return list(reversed(self.shell.history.entries))

    def store_string(self, string: str) -> None:
        self.shell.history.add(string)


def location_line(shell: Shell) -> FormattedText:
    """The current place in yellow, with rwx flags (green granted, red denied) for a folder on a disk."""
    here = shell.vfs.here()
    parts: list[tuple[str, str]] = [("class:location", here.virtual)]
    try:
        info = shell.vfs.stat(here)
    except Exception:
        info = None
    if info is not None and not here.is_root:
        flags = (("r", info.readable), ("w", info.writable), ("x", info.executable)) if here.mount != TABLET else \
            (("r", True), ("w", True), ("x", True))
        parts.append(("", " "))
        parts += [(("class:granted" if ok else "class:denied"), letter if ok else "-") for letter, ok in flags]
    return FormattedText(parts)


def make_session(shell: Shell, engine: CompletionEngine, toolbar: ToolbarState | None = None, **io: Any) -> PromptSession[str]:
    toolbar = toolbar or ToolbarState()
    bindings = KeyBindings()

    @bindings.add("escape", filter=has_completions, eager=True)
    def _close_menu(event: Any) -> None:
        event.current_buffer.cancel_completion()

    def message() -> FormattedText:
        return FormattedText([*location_line(shell), ("", "\n"), ("class:prompt", "> ")])

    return PromptSession(
        message, completer=ShellCompleter(engine, toolbar), auto_suggest=HistorySuggester(engine), history=SavedHistory(shell),
        complete_while_typing=False, complete_style=CompleteStyle.MULTI_COLUMN, reserve_space_for_menu=6, key_bindings=bindings,
        bottom_toolbar=lambda: toolbar.text or None, style=STYLE, enable_history_search=False, mouse_support=False, **io)


# ---------------------------------------------------------------------- the screen
class Screen:
    """Rich printing, a spinner, and the question `rm -r` asks."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console(highlight=False, soft_wrap=True)
        self.error_console = Console(stderr=True, highlight=False, soft_wrap=True)
        self._status: Any = None

    def out(self, line: Line) -> None:
        self.console.print(Text.assemble(*line.segments), overflow="ignore", crop=False)

    def err(self, message: str) -> None:
        self.error_console.print(Text(message, style="red"), overflow="ignore", crop=False)

    def note(self, message: str, style: str = "dim") -> None:
        self.console.print(Text(message, style=style), overflow="ignore", crop=False)

    def spinner(self, label: str | None) -> None:
        if label is None:
            if self._status is not None:
                self._status.stop()
                self._status = None
            return
        if not self.console.is_terminal:
            return
        if self._status is None:
            self._status = self.console.status(label)
            self._status.start()
        else:
            self._status.update(label)

    def confirm(self, question: str) -> bool:
        self.spinner(None)
        try:
            return input(question).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def width(self) -> int:
        return shutil.get_terminal_size((80, 24)).columns


def unicode_ok() -> bool:
    return (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "") in ("utf8", "utf16", "utf32")


def clean_temp(folder: Path, age: float = TEMP_AGE) -> int:
    """Remove files the console left in its temp folder more than a day ago (downloads it opened)."""
    removed = 0
    try:
        for item in folder.iterdir():
            if item.is_file() and time.time() - item.stat().st_mtime > age:
                item.unlink()
                removed += 1
    except OSError:
        pass
    return removed


# ---------------------------------------------------------------------- building and running
def build_shell(runtime: Runtime, screen: Screen, vfs: Vfs | None = None) -> tuple[Shell, CompletionEngine]:
    settings, platform = runtime.settings, runtime.platform
    remarkable = None
    command = find_rmapi(settings.rmapi)
    if command is not None:
        runner = RmapiRunner(command)
        runner.activity = screen.spinner
        remarkable = RemarkableBackend(runner, temp_root=str(settings.data.tmp))
    if vfs is None:
        vfs = Vfs(Resolver(platform, remarkable=remarkable))
    shell = new_shell(vfs, platform, settings.data, remarkable=remarkable, env=dict(os.environ))
    shell.out, shell.err, shell.confirm = screen.out, screen.err, screen.confirm
    shell.spinner, shell.width, shell.unicode = screen.spinner, screen.width, unicode_ok()
    clean_temp(settings.data.tmp)
    return shell, CompletionEngine(shell)


def run_command(shell: Shell, line: str, screen: Screen) -> int:
    """One typed line, with Ctrl+C meaning 'stop this command' and never 'leave the console'."""
    shell.vfs.cancel = CancelToken()  # a fresh token for each command
    try:
        return run_line(shell, line)
    except KeyboardInterrupt:
        shell.vfs.cancel.cancel()
        screen.spinner(None)
        screen.err("interrupted")
        shell.last_status = 130
        return 130
    except Cancelled:
        screen.spinner(None)
        screen.err("interrupted")
        return 130
    finally:
        screen.spinner(None)


def agent_hook(runtime: Runtime, shell: Shell, options: AgentOptions, screen: Screen) -> None:
    """Close the prompt's world, run the full-screen agent in this process, come back. (Phase 4 provides the agent.)"""
    from .agent_app import run_agent

    run_agent(runtime, shell, options)


def handle_request(runtime: Runtime, shell: Shell, screen: Screen) -> bool:
    """Do what a command asked of the loop. Returns False when the shell must end."""
    request, shell.request = shell.request, None
    if request is None:
        return True
    if request.kind == "exit":
        return False
    if request.kind == "clear":
        screen.console.clear()
    elif request.kind == "reload":
        try:
            runtime.platform.desktop.restart_console(sys.argv[1:])
        except DesktopError as error:
            screen.err(f"reload: {error}")
            return True
        return False
    elif request.kind == "agent":
        agent_hook(runtime, shell, request.agent or AgentOptions(), screen)
    return True


def read_plain_lines(prompt: Callable[[], str]) -> Iterator[str]:
    """Lines from a source that is not a terminal (a pipe, a file): no menu, no ghost text."""
    while True:
        try:
            yield prompt()
        except EOFError:
            return


def run_loop(runtime: Runtime, shell: Shell, engine: CompletionEngine, screen: Screen, session: PromptSession[str] | None) -> int:
    for note in runtime.notes:
        screen.note(note, "yellow" if note.startswith("Warning") else "dim")
    if session is None:
        lines: Iterable[str] = read_plain_lines(lambda: input(""))
    else:
        def interactive() -> Iterator[str]:
            while True:
                try:
                    yield session.prompt()
                except KeyboardInterrupt:
                    continue  # Ctrl+C at the prompt abandons the line
                except EOFError:
                    return  # Ctrl+D leaves
        lines = interactive()
    for line in lines:
        if not line.strip():
            continue
        status = run_command(shell, line, screen)
        log.debug("command finished: status %s", status)
        if shell.request is None or shell.request.kind != "clear":
            screen.console.print()
        if not handle_request(runtime, shell, screen):
            break
    return shell.last_status


def run_shell(runtime: Runtime) -> int:
    screen = Screen()
    shell, engine = build_shell(runtime, screen)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    session = make_session(shell, engine) if interactive else None
    try:
        return run_loop(runtime, shell, engine, screen, session)
    finally:
        screen.spinner(None)

