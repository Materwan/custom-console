"""Terminal UI of the agent console.

Behaviour
---------
* The input line is pinned at the bottom of the terminal.
* While the agent answers, its output streams as **plain text** in a live area
  just above the input. That area is part of the prompt_toolkit layout, not of
  the terminal scrollback, so when the answer is complete it simply disappears
  and the turn is printed again, properly rendered as **Markdown**, into the
  normal scrollback.
* Permission questions appear in the same place and are answered in the same
  input line (no second prompt fighting with the display).

The agent itself runs in a worker thread (`turn_runner`); everything that
touches the UI runs on the asyncio loop of the main thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Set

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console, RenderableType
from rich.text import Text

from .permissions import is_yes
from .render import live_lines, render_turn, wrap_line
from .turn import STYLE_ERROR, TurnStats, TurnView

TurnRunner = Callable[[TurnView, threading.Event], Optional[TurnStats]]

UI_ROWS = 2  # header rule + input line
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
HELP_TEXT = (
    "/bye     leave the agent (or Ctrl+D)\n"
    "/clear   clear the screen\n"
    "/help    show this help\n"
    "Ctrl+C   stop the answer in progress"
)

STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "note": "ansibrightblack italic",
        "tool": "ansibrightblack",
        "permission": "ansiyellow",
        "error": "ansired",
        "rule": "ansibrightblack",
        "status": "ansicyan",
        "question": "bold ansiyellow",
        "input-prompt": "bold ansicyan",
    }
)


@dataclass
class _Question:
    """A permission question waiting for the user's answer."""

    info: str
    future: "asyncio.Future[bool]"

    def resolve(self, answer: bool) -> None:
        if not self.future.done():
            self.future.set_result(answer)


class AgentScreen:
    def __init__(
        self,
        *,
        title: str,
        console: Console,
        turn_runner: TurnRunner,
        banner: Optional[RenderableType] = None,
        history: Optional[History] = None,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
    ) -> None:
        self.title = title
        self.console = console
        self.banner = banner
        self._turn_runner = turn_runner

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set["asyncio.Future"] = set()
        self._view: Optional[TurnView] = None
        self._busy = False
        self._cancel = threading.Event()
        self._question: Optional[_Question] = None
        self._live_rows = 0  # rows taken by the live area at the last render

        self._buffer = Buffer(
            history=history or InMemoryHistory(),
            accept_handler=self._accept,
            multiline=False,
        )
        self._app = self._build_app(input, output)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_app(self, input: Optional[Input], output: Optional[Output]) -> Application:
        live = ConditionalContainer(
            Window(FormattedTextControl(self._live_fragments), dont_extend_height=True),
            filter=Condition(lambda: self._view is not None),
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

        return Application(
            layout=Layout(HSplit([live, question, header, entry]), focused_element=entry),
            key_bindings=keys,
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
            refresh_interval=0.1,  # animates the spinner
            input=input,
            output=output,
        )

    def _size(self):
        return self._app.output.get_size()

    def _question_lines(self, width: int) -> List[str]:
        if self._question is None:
            return []
        lines: List[str] = []
        for logical in ("? " + self._question.info).split("\n"):
            lines.extend(wrap_line(logical, width))
        return lines

    def _live_fragments(self):
        view = self._view
        if view is None:
            return []
        size = self._size()
        width = max(1, size.columns - 1)  # never write in the last column
        room = size.rows - UI_ROWS - len(self._question_lines(width)) - 1
        lines = live_lines(view, width, max(3, room))
        self._live_rows = len(lines)

        fragments = []
        for index, (style, text) in enumerate(lines):
            if index:
                fragments.append(("", "\n"))
            fragments.append((style, text))
        return fragments

    def _question_fragments(self):
        lines = self._question_lines(max(1, self._size().columns - 1))
        return [("class:question", "\n".join(lines))]

    def _header_fragments(self):
        if self._question is not None:
            state = "waiting for your answer"
        elif self._busy and self._cancel.is_set():
            state = "stopping..."
        elif self._busy:
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            state = f"{frame} {self._view.activity if self._view else 'working'}"
        else:
            state = "Enter: send · /help · Ctrl+D: quit"

        left = f"── {self.title} ── "
        used = get_cwidth(left) + get_cwidth(state) + 1
        fill = max(0, self._size().columns - 1 - used)
        return [("class:rule", left), ("class:status", state + " "), ("class:rule", "─" * fill)]

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
    # Output above the input line
    # ------------------------------------------------------------------ #

    def _capture(self, *renderables: RenderableType) -> str:
        """Render to a string (width-aware) so that rows can be counted."""
        with self.console.capture() as captured:
            for renderable in renderables:
                self.console.print(renderable)
        return captured.get()

    async def _print_above(self, render: Callable[[], str], pad_to: int = 0) -> None:
        """Print `render()` above the UI, which is erased and redrawn around it.

        `pad_to` keeps the input pinned to the bottom: when the live area that
        was just removed was taller than the text replacing it, the difference
        is filled with blank rows.
        """

        def work() -> None:
            text = render()
            missing = pad_to - text.count("\n")
            if missing > 0:
                text += "\n" * missing
            self.console.file.write(text)
            self.console.file.flush()

        await run_in_terminal(work)

    def _reset_screen(self) -> None:
        """Clear the terminal, show the banner and push the input to the bottom."""
        self.console.clear()
        text = self._capture(self.banner) if self.banner is not None else ""
        pad = max(0, self._size().rows - text.count("\n") - UI_ROWS)
        self.console.file.write(text + "\n" * pad)
        self.console.file.flush()

    # ------------------------------------------------------------------ #
    # Commands and turns (event loop)
    # ------------------------------------------------------------------ #

    async def _command(self, text: str) -> None:
        name = text.split()[0].lower()
        if name in ("/bye", "/exit", "/quit"):
            self._app.exit()
        elif name == "/clear":
            await run_in_terminal(self._reset_screen)
        elif name == "/help":
            await self._print_above(lambda: self._capture(Text(HELP_TEXT, style="dim")))
        else:
            message = Text(f"Unknown command: {name} (try /help)", style="red")
            await self._print_above(lambda: self._capture(message))

    def _finish(self, view: TurnView) -> str:
        """Called once the UI is erased: drop the live area, return the final text."""
        self._view = None
        return self._capture(render_turn(view))

    async def _run_turn(self, text: str) -> None:
        assert self._loop is not None
        view = TurnView(text)
        self._cancel = threading.Event()
        self._view = view
        self._busy = True
        self._app.invalidate()
        try:
            view.stats = await self._loop.run_in_executor(None, self._turn_runner, view, self._cancel)
        except Exception as error:  # the runner should not raise, but never hang the UI
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
        finally:
            rows = self._live_rows
            try:
                await self._print_above(lambda: self._finish(view), pad_to=rows)
            finally:
                self._busy = False
                self._view = None

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
        self._reset_screen()
        await self._app.run_async()

    def run(self) -> None:
        asyncio.run(self.run_async())
