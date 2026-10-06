"""Terminal UI of the agent console.

Behaviour
---------
* Full-screen: the conversation lives in a scrollable pane of its own, and the
  header rule and the input line are **fixed at the bottom of the window**, whatever
  the scroll position (mouse wheel, PageUp / PageDown). New output follows the
  bottom unless you scrolled up.
* While the agent answers, its turn is rendered live in the pane; permission
  questions appear just above the input and are answered in the input line.
* ``/commands`` are provided by the caller (`SlashCommand`); ``/help``, ``/clear``
  and ``/bye`` belong to the screen. Typing ``/`` shows a completion menu.
* When the screen is left, the conversation is written back to the normal terminal
  so that it stays in the scrollback.

The agent itself runs in a worker thread (`turn_runner`); everything that
touches the UI runs on the asyncio loop of the main thread.
"""

from __future__ import annotations

import asyncio
import io
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Union

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    CompletionsMenu,
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

from .permissions import is_yes
from .render import render_turn, wrap_line
from .turn import STYLE_ERROR, TurnStats, TurnView

TurnRunner = Callable[[TurnView, threading.Event], Optional[TurnStats]]
CommandOutput = Union[None, str, RenderableType]

UI_ROWS = 2  # header rule + input line
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WHEEL_LINES = 3

STYLE = Style.from_dict(
    {
        "input-prompt": "bold ansicyan",
        "rule": "ansibrightblack",
        "status": "ansicyan",
        "hint": "ansiyellow",
        "question": "bold ansiyellow",
        "completion-menu": "bg:ansibrightblack ansiwhite",
        "completion-menu.completion.current": "bg:ansicyan ansiblack",
    }
)


@dataclass(frozen=True)
class SlashCommand:
    """A command typed as ``/name args``.

    `handler(args)` runs in a worker thread (it may block on the network) and
    returns what to show: a string, a rich renderable, or None.
    `choices()` are the suggestions for the first argument; it must be cheap.
    """

    name: str
    usage: str
    summary: str
    handler: Callable[[str], CommandOutput]
    choices: Callable[[], Sequence[str]] = lambda: ()


# Commands that belong to the screen itself (they act on the UI, not on the agent).
BUILTINS = (
    ("clear", "", "clear the screen"),
    ("help", "", "show this help"),
    ("bye", "", "leave the agent (or Ctrl+D)"),
)


@dataclass
class _Question:
    """A permission question waiting for the user's answer."""

    info: str
    future: "asyncio.Future[bool]"

    def resolve(self, answer: bool) -> None:
        if not self.future.done():
            self.future.set_result(answer)


class SlashCompleter(Completer):
    """Completes ``/name`` then the first argument of that command."""

    def __init__(self, commands: Callable[[], Dict[str, SlashCommand]]):
        self._commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        words = text.split(" ")
        commands = self._commands()
        if len(words) == 1:
            typed = words[0][1:].lower()
            for name, command in commands.items():
                if name.startswith(typed):
                    yield Completion(
                        f"/{name}",
                        start_position=-len(words[0]),
                        display_meta=command.summary,
                    )
        elif len(words) == 2:
            command = commands.get(words[0][1:].lower())
            if command is not None:
                typed = words[1].lower()
                for choice in command.choices():
                    if choice.lower().startswith(typed):
                        yield Completion(choice, start_position=-len(words[1]))


class _PaneControl(FormattedTextControl):
    """The conversation pane: a text control that reports mouse events (the wheel scrolls)."""

    def __init__(self, text, on_mouse, **options):
        super().__init__(text, **options)
        self._on_mouse = on_mouse

    def mouse_handler(self, mouse_event: MouseEvent):
        return self._on_mouse(mouse_event)


class AgentScreen:
    def __init__(
        self,
        *,
        title: str,
        turn_runner: TurnRunner,
        banner: Optional[RenderableType] = None,
        commands: Sequence[SlashCommand] = (),
        history: Optional[History] = None,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
        color_system: Optional[str] = "standard",
    ) -> None:
        self.title = title
        self.banner = banner
        self.commands: Dict[str, SlashCommand] = {command.name: command for command in commands}
        self._turn_runner = turn_runner
        self._color_system = color_system

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set["asyncio.Future"] = set()
        self._view: Optional[TurnView] = None
        self._busy = False
        self._working = ""  # what a running command is doing, for the header
        self._cancel = threading.Event()
        self._question: Optional[_Question] = None

        # The conversation: finished blocks (rendered once per width) + the turn in progress.
        self._blocks: List[RenderableType] = [banner] if banner is not None else []
        self._rendered: Dict[int, List[str]] = {}  # width -> ANSI text of each finished block
        self._live_key: object = None
        self._live_text = ""
        self._follow = True
        self._top = 0  # first visible line of the conversation
        self._total = 0  # lines of the conversation at the last render

        self._buffer = Buffer(
            history=history or InMemoryHistory(),
            accept_handler=self._accept,
            completer=SlashCompleter(lambda: self.commands),
            complete_while_typing=True,
            multiline=False,
        )
        self._app = self._build_app(input, output)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_app(self, input: Optional[Input], output: Optional[Output]) -> Application:
        self._pane = Window(
            _PaneControl(
                self._pane_text,
                self._pane_mouse,
                get_cursor_position=lambda: Point(0, self._top),  # always inside the view
                focusable=False,
            ),
            get_vertical_scroll=lambda window: self._top,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        question = ConditionalContainer(
            Window(FormattedTextControl(self._question_fragments), dont_extend_height=True),
            filter=Condition(lambda: self._question is not None),
        )
        header = Window(FormattedTextControl(self._header_fragments), height=1)
        entry = Window(
            BufferControl(self._buffer, input_processors=[BeforeInput(self._prompt_fragments)]),
            height=1,
        )

        keys = KeyBindings()

        @keys.add("c-c")
        def _interrupt(event) -> None:
            self._interrupt()

        @keys.add("c-d")
        def _quit(event) -> None:
            if not self._busy and self._question is None and not self._buffer.text:
                event.app.exit()

        @keys.add("pageup")
        def _page_up(event) -> None:
            self._scroll_by(-self._page())

        @keys.add("pagedown")
        def _page_down(event) -> None:
            self._scroll_by(self._page())

        body = FloatContainer(
            HSplit([self._pane, question, header, entry]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1))],
        )
        return Application(
            layout=Layout(body, focused_element=entry),
            key_bindings=keys,
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.1,  # animates the spinner and the live turn
            input=input,
            output=output,
        )

    def _size(self):
        return self._app.output.get_size()

    def _width(self) -> int:
        return max(20, self._size().columns - 1)  # never write in the last column

    def _page(self) -> int:
        info = self._pane.render_info
        return max(1, (info.window_height if info else self._size().rows - UI_ROWS) - 1)

    # -- rendering ----------------------------------------------------------- #

    def _render(self, renderables: Sequence[RenderableType], width: int, ansi: bool = True) -> str:
        """Render to text `width` cells wide (ANSI-styled, or plain)."""
        console = Console(
            file=io.StringIO(),
            width=width,
            force_terminal=ansi,
            color_system=self._color_system if ansi else None,
            legacy_windows=False,
        )
        for renderable in renderables:
            console.print(renderable)
        return console.file.getvalue()

    def _block_texts(self, width: int) -> List[str]:
        """ANSI text of every finished block (cached: only new blocks are rendered)."""
        texts = self._rendered.setdefault(width, [])
        for block in self._blocks[len(texts) :]:
            texts.append(self._render([block], width))
        return texts

    def _live(self, width: int) -> str:
        view = self._view
        if view is None:
            return ""
        key = (width, tuple((s.kind, len(s.text), s.style) for s in view.snapshot()))
        if key != self._live_key:  # re-render the turn only when it changed
            self._live_key, self._live_text = key, self._render([render_turn(view)], width)
        return self._live_text

    def _pane_text(self):
        width = self._width()
        text = "".join(self._block_texts(width)) + self._live(width)
        self._total = text.count("\n")
        info = self._pane.render_info
        height = info.window_height if info else max(1, self._size().rows - UI_ROWS)
        limit = max(0, self._total - height)
        self._top = limit if self._follow else min(self._top, limit)
        return ANSI(text)

    def transcript_text(self, width: int = 80) -> str:
        """The whole conversation as plain text (what the user has seen)."""
        parts = list(self._blocks)
        if self._view is not None:
            parts.append(render_turn(self._view))
        return self._render(parts, width, ansi=False)

    # -- scrolling ----------------------------------------------------------- #

    def _scroll_by(self, lines: int) -> None:
        info = self._pane.render_info
        height = info.window_height if info else max(1, self._size().rows - UI_ROWS)
        limit = max(0, self._total - height)
        self._top = max(0, min(limit, self._top + lines))
        self._follow = self._top >= limit
        self._app.invalidate()

    def _pane_mouse(self, event: MouseEvent):
        if event.event_type == MouseEventType.SCROLL_UP:
            self._scroll_by(-WHEEL_LINES)
        elif event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll_by(WHEEL_LINES)
        else:
            return NotImplemented
        return None

    # -- fixed bottom rows --------------------------------------------------- #

    def _question_lines(self, width: int) -> List[str]:
        if self._question is None:
            return []
        lines: List[str] = []
        for logical in ("? " + self._question.info).split("\n"):
            lines.extend(wrap_line(logical, width))
        return lines

    def _question_fragments(self):
        lines = self._question_lines(self._width())
        return [("class:question", "\n".join(lines))]

    def _header_fragments(self):
        if self._question is not None:
            state = "waiting for your answer"
        elif self._busy and self._cancel.is_set():
            state = "stopping..."
        elif self._busy:
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            state = f"{frame} {self._view.activity if self._view else (self._working or 'working')}"
        elif not self._follow:
            state = "↑ scrolled · PgDn: back to the end"
        else:
            state = "Enter: send · /help · PgUp/PgDn: scroll · Ctrl+D: quit"

        left = f"── {self.title} ── "
        room = self._size().columns - 1 - get_cwidth(left) - 1
        if get_cwidth(state) > room:  # narrow window: keep the start, mark the cut
            state = state[: max(0, room - 1)] + "…"
        used = get_cwidth(left) + get_cwidth(state) + 1
        fill = max(0, self._size().columns - 1 - used)
        style = "class:hint" if not self._follow and not self._busy else "class:status"
        return [("class:rule", left), (style, state + " "), ("class:rule", "─" * fill)]

    def _prompt_fragments(self):
        label = "Accept (Y|n) > " if self._question is not None else "> "
        return [("class:input-prompt", label)]

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #

    def _accept(self, buffer: Buffer) -> bool:
        """Enter pressed. Returns True to keep the text in the input line."""
        text = buffer.text.strip()
        if self._question is not None:
            self._question.resolve(is_yes(text))
            return False
        if self._busy:
            return True  # keep what was typed; press Enter again once the agent is done
        if not text:
            return False
        self._spawn(self._command(text) if text.startswith("/") else self._run_turn(text))
        return False

    def _interrupt(self) -> None:
        if self._question is not None:
            self._question.resolve(False)
        if self._busy:
            self._cancel.set()
            self._app.invalidate()
        elif self._buffer.text:
            self._buffer.reset()

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: "asyncio.Future") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._app.exit(exception=task.exception())  # a bug must not vanish silently

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #

    def print(self, renderable: RenderableType) -> None:
        """Add a block to the conversation and follow it (event loop thread only)."""
        self._blocks.append(renderable)
        self._follow = True
        self._app.invalidate()

    def _help(self) -> RenderableType:
        table = Table.grid(padding=(0, 2))
        for name, usage, summary in BUILTINS:
            table.add_row(Text(f"/{name} {usage}".rstrip(), style="cyan"), Text(summary))
        for command in self.commands.values():
            table.add_row(Text(f"/{command.name} {command.usage}".rstrip(), style="cyan"), Text(command.summary))
        table.add_row(Text("Ctrl+C", style="cyan"), Text("stop the answer in progress"))
        table.add_row(Text("PgUp/PgDn", style="cyan"), Text("scroll (also the mouse wheel; Shift+drag selects text)"))
        return table

    def request_clear(self) -> None:
        """Clear the conversation view; callable from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._clear_now)

    def _clear_now(self) -> None:
        self._reset_screen()
        self._app.invalidate()

    def _reset_screen(self) -> None:
        self._blocks = [self.banner] if self.banner is not None else []
        self._rendered.clear()
        self._follow = True

    # ------------------------------------------------------------------ #
    # Commands and turns (event loop)
    # ------------------------------------------------------------------ #

    async def _command(self, text: str) -> None:
        name, _, args = text[1:].partition(" ")
        name = name.lower()
        if name in ("bye", "exit", "quit"):
            self._app.exit()
        elif name == "clear":
            self._reset_screen()
        elif name == "help":
            self.print(self._help())
        elif name in self.commands:
            await self._run_command(self.commands[name], args.strip())
        else:
            self.print(Text(f"Unknown command: /{name} (try /help)", style="red"))
        self._app.invalidate()

    async def _run_command(self, command: SlashCommand, args: str) -> None:
        assert self._loop is not None
        self._busy, self._working = True, f"/{command.name}"
        self._app.invalidate()
        try:
            output = await self._loop.run_in_executor(None, command.handler, args)
        except Exception as error:  # a command must not take the console down
            output = Text(f"/{command.name}: {error}", style="red")
        finally:
            self._busy, self._working = False, ""
        if isinstance(output, str):
            output = Text(output)
        if output is not None:
            self.print(output)

    async def _run_turn(self, text: str) -> None:
        assert self._loop is not None
        view = TurnView(text)
        self._cancel = threading.Event()
        self._view = view
        self._busy = True
        self._follow = True
        self._app.invalidate()
        try:
            view.stats = await self._loop.run_in_executor(None, self._turn_runner, view, self._cancel)
        except Exception as error:  # the runner should not raise, but never hang the UI
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
        finally:
            self._blocks.append(render_turn(view))  # the finished turn replaces the live one
            self._view = None
            self._busy = False
            self._app.invalidate()

    # ------------------------------------------------------------------ #
    # Permission questions (called from the agent's worker thread)
    # ------------------------------------------------------------------ #

    def ask_permission(self, info: str) -> bool:
        """Block the calling (non-UI) thread until the user answers."""
        if self._loop is None:
            raise RuntimeError("The screen is not running.")
        return asyncio.run_coroutine_threadsafe(self._ask(info), self._loop).result()

    async def _ask(self, info: str) -> bool:
        question = _Question(info, asyncio.get_running_loop().create_future())
        self._question = question
        self._follow = True
        self._app.invalidate()
        try:
            return await question.future
        finally:
            self._question = None
            self._app.invalidate()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._app.run_async()

    def run(self) -> None:
        asyncio.run(self.run_async())
        # The full-screen view is gone: leave the conversation in the normal scrollback.
        try:
            width = max(20, self._app.output.get_size().columns - 1)
            sys.stdout.write(self._render(self._blocks, width))
            sys.stdout.flush()
        except Exception:
            pass
