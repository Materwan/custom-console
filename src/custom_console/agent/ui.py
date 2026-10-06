"""Terminal UI of the agent console.

Behaviour
---------
* The input is pinned at the bottom of the terminal. It grows with what is
  typed (long lines wrap, Shift+Tab starts a new line) and Enter sends it.
* While the agent answers, what is final (finished paragraphs, finished tool
  lines) is printed into the normal scrollback as it comes, rendered as
  **Markdown**, so the terminal can be scrolled meanwhile. What still changes
  is shown as plain text in a live area just above the input (see `render`).
* The rule above the input shows the activity, the time spent, the tokens
  generated so far and the progress of the agent's checklist.
* Tool calls take one line each. Ctrl+O (or /details) shows what they hide
  (the diff of a file change, a command's output...) from then on; Ctrl+T (or
  /transcript) opens the conversation in a full-screen viewer where each tool
  line unfolds with Enter or a click.
* Permission questions, checklists (`ask_menu`) and the agent's questions
  (`ask_choice`) appear in the same place and are answered there (see
  `overlays`). What the user was typing is put aside meanwhile, and comes back.
* Typing ``/`` lists the slash commands (and their arguments) above the input.
* A message sent while the agent works is queued, and sent when it is done.

The agent itself runs in a worker thread (`turn_runner`); everything that
touches the UI runs on the asyncio loop of the main thread.

Layout height
-------------
prompt_toolkit redraws a non-full-screen layout from its first row, so when the
layout gets shorter the input line would drift away from the bottom of the
terminal. The layout therefore never shrinks while something is on screen: a
filler at its top keeps the height reached (`_peak`). Text printed above the
UI takes the place of filler rows, which keeps the input on the last row.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Set, Tuple

from prompt_toolkit.application import Application, in_terminal, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, ThreadedCompleter, merge_completers
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.completion import generate_completions
from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console, RenderableType

from .overlays import ChoiceQuestion, Menu, MenuItem, Question, TextQuestion, parse_answer
from .permissions import Decision
from .questions import Answer, Choice
from .render import StyledLine, TurnPrinter, live_lines, wrap_line
from .slash import CommandResult, Renderer, SlashCompleter, SlashRegistry
from .turn import STYLE_ERROR, TurnStats, TurnView
from .usage import format_count

__all__ = ["AgentScreen", "MenuItem", "add_basic_commands", "parse_answer"]

TurnRunner = Callable[[TurnView, threading.Event], Optional[TurnStats]]
Progress = Tuple[int, int, str]  # checklist: items done, items, the item in progress

UI_ROWS = 2  # header rule + input line (when the input holds one line)
MAX_INPUT_ROWS = 10
SUGGESTION_ROWS = 6
STREAM_PERIOD = 0.15  # seconds between two looks for finished output to print
FLASH_SECONDS = 2.5
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
IDLE_HINTS = ("Enter: send · @file attaches · !cmd runs · /help", "Enter: send · /help", "/help")

KEYS_HELP = (
    "Keys: Enter sends (queued while the agent works) · Shift+Tab new line · Tab completes · "
    "Ctrl+O tool details · Ctrl+T transcript · Ctrl+C stops the answer · Ctrl+D leaves\n"
    "In a message: @path attaches a file or folder. A line starting with ! runs a command yourself "
    "(its output goes with your next message)"
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
        "flash": "bold ansigreen",
        "question": "bold ansiyellow",
        "question-hint": "ansiyellow italic",
        "input-prompt": "bold ansicyan",
        "diff-add": "ansigreen",
        "diff-del": "ansired",
        "diff-hunk": "ansicyan",
        "diff-ctx": "ansibrightblack",
        "detail": "ansibrightblack",
        "thinking": "ansibrightblack italic",
        "todo": "",
        "todo-active": "bold ansiyellow",
        "todo-done": "ansibrightblack",
        "suggestion": "ansibrightblack",
        "suggestion-selected": "reverse",
        "suggestion-meta": "ansibrightblack italic",
        "menu-title": "bold ansiyellow",
        "menu-on": "",
        "menu-off": "ansibrightblack",
        "menu-cursor": "reverse",
        "menu-hint": "ansibrightblack italic",
        "choice-description": "ansibrightblack",
    }
)


def format_elapsed(seconds: float) -> str:
    """``8.4s``, ``2m05s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _fragments(lines: List[StyledLine]):
    fragments = []
    for index, (style, text) in enumerate(lines):
        if index:
            fragments.append(("", "\n"))
        fragments.append((style, text))
    return fragments


def add_basic_commands(registry: SlashRegistry) -> None:
    """/bye, /clear, /help and /transcript: what every screen needs, whatever else is registered."""
    from .slash import SlashCommand

    def help_text() -> str:
        groups: dict = {}
        for command in registry.commands():
            groups.setdefault(command.group, []).append(command)
        lines: List[str] = []
        for group, commands in groups.items():
            lines.append(f"{group}:")
            width = max(len(c.name + " " + c.usage) for c in commands)
            for command in commands:
                lines.append(f"  /{(command.name + ' ' + command.usage).strip().ljust(width)}  {command.summary}")
        lines.append("")
        lines.append(KEYS_HELP)
        return "\n".join(lines)

    registry.add(SlashCommand("help", "show this help", lambda _: CommandResult(registry.renderer.text(help_text(), "dim"))))
    registry.add(SlashCommand("clear", "clear the screen", lambda _: CommandResult(clear_screen=True)))
    registry.add(SlashCommand("transcript", "the whole conversation, tool lines unfoldable (or Ctrl+T)", lambda _: CommandResult(transcript=True)))
    registry.add(SlashCommand("bye", "leave the agent (or Ctrl+D)", lambda _: CommandResult(quit=True)), "exit", "quit")


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
        commands: Optional[SlashRegistry] = None,
        status: Callable[[], str] = lambda: "",
        todos: Callable[[], Progress] = lambda: (0, 0, ""),
        transcript: Optional[Callable[[], List[TurnView]]] = None,
        completer: Optional[Completer] = None,
        bang: Optional[Callable[[str], CommandResult]] = None,
    ) -> None:
        """`completer` completes what is typed besides /commands (@paths); `bang(command)` runs a
        line that starts with ``!``."""
        self.title = title
        self.console = console
        self.banner = banner
        self._status = status  # right side of the header (e.g. context usage)
        self._todos = todos  # progress of the agent's checklist
        self._turn_runner = turn_runner

        if commands is None:
            commands = SlashRegistry(Renderer(console))
            add_basic_commands(commands)
        self.commands = commands

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: Set["asyncio.Future"] = set()
        self._view: Optional[TurnView] = None
        self._printer: Optional[TurnPrinter] = None
        self._busy = False
        self._busy_label = "working"
        self._cancel = threading.Event()
        self._question: Optional[Question] = None
        self._menu: Optional[Menu] = None
        self._choice: Optional[ChoiceQuestion] = None
        self._text: Optional[TextQuestion] = None
        self._queue: List[str] = []  # messages sent while the agent was busy, sent when it is done
        self._details = False  # tool lines are printed with what they hide
        self._flash: Tuple[str, float] = ("", 0.0)  # a short message in the header, until a time
        self._peak = 0  # tallest the layout above the header has been (see module docstring)
        self._finished: List[TurnView] = []  # this screen's turns (the default transcript)
        self._notices: List[str] = []  # shown once the screen runs (see `notify`)
        self._notices_lock = threading.Lock()
        self.viewer = None  # the transcript viewer, while it is open
        self._transcript = transcript or (lambda: list(self._finished))
        self._bang = bang
        self._stoppable = False  # the command running now (a !command) stops on Ctrl+C

        self._buffer = Buffer(
            history=history or InMemoryHistory(),
            accept_handler=self._accept,
            multiline=True,  # Enter is bound to "send"; Shift+Tab inserts the new lines
            completer=ThreadedCompleter(
                merge_completers([SlashCompleter(self.commands), *([completer] if completer else [])])
            ),
            complete_while_typing=Condition(self._completing),
            on_text_changed=self._text_changed,
        )
        self._app = self._build_app(input, output)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _completing(self) -> bool:
        if not self._overlay_free():
            return False
        text = self._buffer.document.text_before_cursor
        return text.startswith("/") or self._buffer.document.get_word_before_cursor(WORD=True).startswith("@")

    def _overlay_free(self) -> bool:
        """No question, menu or choice is waiting for the user."""
        return self._question is None and self._menu is None and self._choice is None and self._text is None

    def _build_app(self, input: Optional[Input], output: Optional[Output]) -> Application:
        filler = ConditionalContainer(
            Window(FormattedTextControl(""), height=lambda: Dimension.exact(self._filler_rows())),
            filter=Condition(lambda: self._filler_rows() > 0),
        )
        live = ConditionalContainer(
            Window(FormattedTextControl(self._live_fragments), dont_extend_height=True),
            filter=Condition(lambda: self._view is not None),
        )
        question = ConditionalContainer(
            Window(FormattedTextControl(self._question_fragments), dont_extend_height=True),
            filter=Condition(lambda: self._question is not None or self._text is not None),
        )
        menu = ConditionalContainer(
            Window(FormattedTextControl(self._menu_fragments), dont_extend_height=True),
            filter=Condition(lambda: self._menu is not None),
        )
        choice = ConditionalContainer(
            Window(FormattedTextControl(lambda: _fragments(self._choice_lines(self._width()))), dont_extend_height=True),
            filter=Condition(lambda: self._choice is not None),
        )
        suggestions = ConditionalContainer(
            Window(FormattedTextControl(self._suggestion_fragments), dont_extend_height=True),
            filter=Condition(lambda: bool(self._suggestion_lines())),
        )
        header = Window(FormattedTextControl(self._header_fragments), height=1)
        masked = Condition(lambda: self._text is not None and self._text.secret)
        entry = Window(
            BufferControl(
                self._buffer,
                input_processors=[BeforeInput(self._prompt_fragments), ConditionalProcessor(PasswordProcessor(), masked)],
            ),
            height=lambda: Dimension.exact(self._input_rows()),
            wrap_lines=True,
            get_line_prefix=self._line_prefix,
        )

        keys = KeyBindings()
        in_menu = Condition(lambda: self._menu is not None)
        in_choice = Condition(lambda: self._choice is not None)
        in_text = Condition(lambda: self._text is not None)
        idle = Condition(lambda: not self._busy and self._overlay_free())

        @keys.add("c-c")
        def _interrupt(event) -> None:
            self._interrupt()

        @keys.add("c-d")
        def _quit(event) -> None:
            if not self._busy and self._overlay_free() and not self._buffer.text:
                event.app.exit()

        @keys.add("enter", filter=~in_menu & ~in_choice & ~in_text)
        def _send(event) -> None:
            event.current_buffer.validate_and_handle()

        # -- a free-text question (an API key) --------------------------------------------- #

        @keys.add("enter", filter=in_text)
        def _text_send(event) -> None:
            if self._text is not None:
                answer = self._buffer.text
                self._buffer.reset()  # not validate_and_handle: the answer must not reach the history
                self._text.resolve(answer)

        @keys.add("escape", filter=in_text, eager=True)
        def _text_cancel(event) -> None:
            if self._text is not None:
                self._buffer.reset()
                self._text.resolve(None)

        @keys.add("up", filter=in_text)
        @keys.add("down", filter=in_text)
        def _text_no_history(event) -> None:
            pass  # the history must not be recalled into a secret

        @keys.add("c-o")
        def _details(event) -> None:
            self.toggle_details()

        @keys.add("c-t", filter=idle)
        def _transcript(event) -> None:
            self._spawn(self._busy_while(self._open_transcript(), "reading the transcript"))

        # -- checklist menu ------------------------------------------------------ #

        @keys.add("<any>", filter=in_menu)
        def _menu_ignore(event) -> None:
            pass  # the input line is not used while a menu is open

        def menu_key(*names: str, eager: bool = False):
            def decorate(function):
                for name in names:  # alternatives, not a key sequence
                    keys.add(name, filter=in_menu, eager=eager)(function)
                return function

            return decorate

        @menu_key("up", "k")
        def _menu_up(event) -> None:
            self._menu_move(-1)

        @menu_key("down", "j")
        def _menu_down(event) -> None:
            self._menu_move(1)

        @menu_key("pageup")
        def _menu_page_up(event) -> None:
            self._menu_move(-self._menu_height())

        @menu_key("pagedown")
        def _menu_page_down(event) -> None:
            self._menu_move(self._menu_height())

        @menu_key("home")
        def _menu_home(event) -> None:
            self._menu_move(-10**6)

        @menu_key("end")
        def _menu_end(event) -> None:
            self._menu_move(10**6)

        @menu_key("space")
        def _menu_toggle(event) -> None:
            if self._menu is not None:
                self._menu.toggle()
                self._app.invalidate()

        @menu_key("a")
        def _menu_toggle_all(event) -> None:
            if self._menu is not None:
                self._menu.toggle_all()
                self._app.invalidate()

        @menu_key("enter")
        def _menu_apply(event) -> None:
            if self._menu is not None:
                self._menu.resolve(self._menu.result())

        @menu_key("escape", eager=True)
        def _menu_cancel(event) -> None:
            if self._menu is not None:
                self._menu.resolve(None)

        # -- the agent's question ------------------------------------------------- #

        @keys.add("<any>", filter=in_choice & Condition(lambda: not self._choice.allow_other))
        def _choice_ignore(event) -> None:
            pass  # options only: nothing to type

        @keys.add("up", filter=in_choice)
        def _choice_up(event) -> None:
            self._choice_move(-1)

        @keys.add("down", filter=in_choice)
        def _choice_down(event) -> None:
            self._choice_move(1)

        @keys.add("pageup", filter=in_choice)
        def _choice_page_up(event) -> None:
            self._choice_move(-self._choice_page())

        @keys.add("pagedown", filter=in_choice)
        def _choice_page_down(event) -> None:
            self._choice_move(self._choice_page())

        @keys.add("home", filter=in_choice & Condition(lambda: not self._choice.on_other()))
        def _choice_home(event) -> None:
            self._choice_move(-10**6)

        @keys.add("end", filter=in_choice & Condition(lambda: not self._choice.on_other()))
        def _choice_end(event) -> None:
            self._choice_move(10**6)

        @keys.add("space", filter=in_choice & Condition(lambda: not self._choice.on_other()))
        def _choice_toggle(event) -> None:
            if self._choice is not None and self._choice.multiple:
                self._choice.toggle()
                self._app.invalidate()

        @keys.add("enter", filter=in_choice)
        def _choice_send(event) -> None:
            if self._choice is not None:
                answer = self._choice.answer(self._buffer.text)
                if answer is not None:
                    self._choice.resolve(answer)

        @keys.add("escape", filter=in_choice, eager=True)
        def _choice_skip(event) -> None:
            if self._choice is not None:
                self._choice.resolve(None)

        # -- completion and new lines ------------------------------------------------ #

        @keys.add("tab")
        def _complete(event) -> None:
            generate_completions(event)

        @keys.add("s-tab", filter=~in_menu)
        def _complete_back_or_new_line(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state:
                buffer.complete_previous()
            else:
                buffer.insert_text("\n")

        return Application(
            layout=Layout(HSplit([filler, live, question, menu, choice, suggestions, header, entry]), focused_element=entry),
            key_bindings=keys,
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
            refresh_interval=0.1,  # animates the spinner and the clock
            input=input,
            output=output,
        )

    def _size(self):
        return self._app.output.get_size()

    # -- sizes ------------------------------------------------------------------------ #

    def _width(self) -> int:
        return max(1, self._size().columns - 1)  # never write in the last column

    def _prompt_width(self) -> int:
        return sum(get_cwidth(text) for _, text in self._prompt_fragments())

    def _input_rows(self) -> int:
        """Rows of the input: its lines, wrapped like the window wraps them."""
        room = max(1, self._size().columns - self._prompt_width())
        lines = self._buffer.text.split("\n")
        rows = 0
        for index, line in enumerate(lines):
            cells = get_cwidth(line) + (1 if index == len(lines) - 1 else 0)  # the cursor at the end
            rows += max(1, -(-cells // room))
        limit = max(1, min(MAX_INPUT_ROWS, (self._size().rows - 1) // 3))
        return min(rows, limit)

    def _line_prefix(self, line_number: int, wrap_count: int):
        """Continuation rows of the input are aligned with its first row."""
        if line_number == 0 and wrap_count == 0:
            return []
        return [("", " " * self._prompt_width())]

    def _question_lines(self, width: int) -> List[StyledLine]:
        asked = self._question or self._text
        if asked is None:
            return []
        lines: List[StyledLine] = []
        for logical in ("? " + asked.info).split("\n"):
            lines.extend(("class:question", piece) for piece in wrap_line(logical, width))
        hint = asked.hint() if isinstance(asked, Question) else ""
        if hint:
            lines.extend(("class:question-hint", piece) for piece in wrap_line(hint, width))
        return lines

    def _suggestion_lines(self) -> List[Tuple[str, str]]:
        """``(style, text)`` rows of the completion list, scrolled to the selection."""
        state = self._buffer.complete_state
        if state is None or not state.completions or not self._overlay_free():
            return []
        completions = state.completions
        selected = state.complete_index
        start = 0 if selected is None else max(0, selected - SUGGESTION_ROWS + 1)
        rows: List[Tuple[str, str]] = []
        width = self._width()
        for offset, completion in enumerate(completions[start : start + SUGGESTION_ROWS]):
            label = "  " + completion.display_text
            meta = completion.display_meta_text
            if meta:
                label += "  " + meta
            is_selected = selected is not None and start + offset == selected
            rows.append(("class:suggestion-selected" if is_selected else "class:suggestion", label[:width]))
        return rows

    def _panel_rows(self, width: int) -> int:
        """Rows of what is shown between the live area and the header."""
        return (
            len(self._question_lines(width))
            + len(self._menu_lines(width))
            + len(self._choice_lines(width))
            + len(self._suggestion_lines())
        )

    def _measure(self) -> int:
        """Rows used by the layout besides the header and the first input row."""
        width = self._width()
        rows = self._panel_rows(width) + self._input_rows() - 1
        if self._view is not None:
            rows += len(live_lines(self._view, width, self._live_budget(width), self._printer))
        return rows

    def _filler_rows(self) -> int:
        rows = self._measure()
        self._peak = min(max(self._peak, rows), max(0, self._size().rows - UI_ROWS))
        return max(0, self._peak - rows)

    def _live_budget(self, width: int) -> int:
        """Rows the live text may take: what the terminal has left."""
        taken = self._panel_rows(width) + self._input_rows() + 1
        return max(3, self._size().rows - taken - 1)

    # -- menu ---------------------------------------------------------------------------- #

    def _menu_height(self) -> int:
        """Rows of the menu's list: what the terminal leaves next to its title and footer."""
        return max(3, self._size().rows - 1 - self._input_rows() - 3)

    def _menu_move(self, delta: int) -> None:
        if self._menu is not None:
            self._menu.move(delta)
            self._app.invalidate()

    def _menu_lines(self, width: int) -> List[Tuple[str, str]]:
        """``(style, text)`` rows of the open menu: title, a window of the list, footer."""
        menu = self._menu
        if menu is None:
            return []
        rows = menu.rows()
        height = min(len(rows), self._menu_height())
        menu.cursor = max(0, min(len(rows) - 1, menu.cursor))
        if menu.cursor < menu.scroll:
            menu.scroll = menu.cursor
        elif menu.cursor >= menu.scroll + height:
            menu.scroll = menu.cursor - height + 1
        menu.scroll = max(0, min(menu.scroll, len(rows) - height))

        label_width = max((len(item.label) for item in menu.items), default=0)
        lines: List[Tuple[str, str]] = [("class:menu-title", ("? " + menu.title)[:width])]
        for index in range(menu.scroll, menu.scroll + height):
            kind, target = rows[index]
            if kind == "group":
                members = menu.members(target)  # type: ignore[arg-type]
                on = sum(1 for item in members if item.checked)
                mark = "[x]" if on == len(members) else "[ ]" if on == 0 else "[-]"
                text = f"{mark} {target}  ({on}/{len(members)})"
                style = "class:menu-on"
            else:
                item = target  # type: ignore[assignment]
                text = f"    [{'x' if item.checked else ' '}] {item.label.ljust(label_width)}"
                if item.detail:
                    text += "  " + item.detail
                style = "class:menu-on" if item.checked else "class:menu-off"
            pointer = "❯ " if index == menu.cursor else "  "
            text = (pointer + text)[:width]
            if index == menu.cursor:
                lines.append(("class:menu-cursor", text.ljust(width)))
            else:
                lines.append((style, text))

        enabled = sum(1 for item in menu.items if item.checked)
        footer = f"  {enabled}/{len(menu.items)} on"
        if menu.scroll > 0:
            footer += f" · ↑ {menu.scroll} more"
        if menu.scroll + height < len(rows):
            footer += f" · ↓ {len(rows) - menu.scroll - height} more"
        lines.append(("class:menu-hint", footer[:width]))
        return lines

    def _menu_fragments(self):
        return _fragments(self._menu_lines(self._width()))

    # -- the agent's question ------------------------------------------------------------ #

    def _choice_move(self, delta: int) -> None:
        if self._choice is not None:
            self._choice.move(delta)
            self._app.invalidate()

    def _text_changed(self, buffer: Buffer) -> None:
        """Typing while the agent asks a question means "another answer"."""
        choice = self._choice
        if choice is not None and choice.allow_other and buffer.text:
            choice.cursor = choice.other_row

    def _choice_room(self, title_rows: int) -> int:
        """Rows the options may take: the terminal minus the title, the hint and the input."""
        return max(3, self._size().rows - 1 - self._input_rows() - title_rows - 1)

    def _choice_page(self) -> int:
        """Rows PageUp/PageDown move by: what the panel shows, less one."""
        if self._choice is None:
            return 1
        self._choice_lines(self._width())
        return max(1, self._choice.visible - 1)

    def _choice_lines(self, width: int) -> List[StyledLine]:
        choice = self._choice
        if choice is None:
            return []
        title: List[StyledLine] = []
        for logical in ("? " + choice.question).split("\n"):
            title.extend(("class:question", piece) for piece in wrap_line(logical, width))

        rows = [(f"{index + 1}. {option.label}", option.description) for index, option in enumerate(choice.options)]
        if choice.allow_other:
            rows.append(("✎ Something else: type it", ""))
        blocks: List[List[StyledLine]] = []
        for index, (label, description) in enumerate(rows):
            pointer = "❯ " if index == choice.cursor else "  "
            mark = ""
            if choice.multiple and index < len(choice.options):
                mark = "[x] " if index in choice.checked else "[ ] "
            elif choice.multiple:
                mark = "    "
            text = (pointer + mark + label)[:width]
            block = [("class:menu-cursor", text.ljust(width)) if index == choice.cursor else ("class:menu-on", text)]
            indent = " " * (len(pointer) + len(mark) + 3)
            for piece in wrap_line(description, max(1, width - len(indent))) if description else []:
                block.append(("class:choice-description", indent + piece))
            blocks.append(block)

        # A window of the rows that keeps the cursor in view (long lists: models...).
        room = self._choice_room(len(title))
        choice.scroll = min(choice.scroll, choice.cursor)
        while choice.scroll < choice.cursor and sum(len(b) for b in blocks[choice.scroll : choice.cursor + 1]) > room:
            choice.scroll += 1
        end, used = choice.scroll, 0
        while end < len(blocks) and used + len(blocks[end]) <= room:
            used += len(blocks[end])
            end += 1
        end = max(end, choice.cursor + 1)
        choice.visible = end - choice.scroll

        if choice.multiple:
            hint = "  ↑↓ move · Space tick · Enter send · Esc skip"
        else:
            hint = "  ↑↓ choose · Enter answer · Esc skip"
        if choice.scroll > 0:
            hint += f" · ↑ {choice.scroll} more"
        if end < len(blocks):
            hint += f" · ↓ {len(blocks) - end} more"
        lines = title + [line for block in blocks[choice.scroll : end] for line in block]
        lines.append(("class:menu-hint", hint[:width]))
        return lines

    # -- fragments ------------------------------------------------------------- #

    def _live_fragments(self):
        view = self._view
        if view is None:
            return []
        width = self._width()
        return _fragments(live_lines(view, width, self._live_budget(width), self._printer))

    def _question_fragments(self):
        return _fragments(self._question_lines(self._width()))

    def _suggestion_fragments(self):
        return _fragments(self._suggestion_lines())

    def _header_states(self) -> List[Tuple[str, str]]:
        """``(style, text)`` candidates for the middle of the header, longest first."""
        flash, until = self._flash
        flashing = [("class:flash", flash)] if flash and time.monotonic() < until else []
        if self._menu is not None:
            texts = [
                "↑↓ move · Space toggle · a all · Enter apply · Esc cancel",
                "Space toggle · Enter apply · Esc cancel",
                "Enter apply",
            ]
        elif self._choice is not None:
            texts = ["the agent asks you a question", "question"] if self._view is not None else ["choose", "?"]
        elif self._text is not None:
            texts = ["Enter: send · Esc: cancel", "Esc: cancel"]
        elif self._question is not None:
            texts = ["waiting for your answer"]
        elif self._busy and self._cancel.is_set():
            texts = ["stopping..."]
        elif self._busy:
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            view = self._view
            label = view.activity if view is not None else self._busy_label
            texts = []
            if view is not None:
                timing = f"{format_elapsed(view.elapsed)} · ↓ {format_count(view.output_tokens)} tokens"
                texts += [f"{frame} {label} · {timing} · {todo}" for todo in self._todo_texts()]
                texts += [f"{frame} {label} · {timing}", f"{frame} {timing}"]
            texts.append(f"{frame} {label}")
            if self._queue:
                texts = [f"{text} · {len(self._queue)} queued" for text in texts[:-1]] + texts[-1:]
        else:
            todos = [todo for todo in self._todo_texts(pending_only=True)][-1:]
            texts = [f"{todo} · {hint}" for todo in todos for hint in IDLE_HINTS] + list(IDLE_HINTS)
        return flashing + [("class:status", text) for text in texts]

    def _todo_texts(self, pending_only: bool = False) -> List[str]:
        """``☑ 2/5 · Writing the tests``, ``☑ 2/5``: the checklist's progress, or nothing."""
        try:
            done, total, active = self._todos()
        except Exception:
            return []
        if not total or (pending_only and done >= total):
            return []
        short = f"☑ {done}/{total}"
        return [f"{short} · {active}", short] if active else [short]

    def _header_fragments(self):
        columns = self._width()
        states = self._header_states()

        left = f"── {self.title} ── "
        try:
            status = self._status()
        except Exception:
            status = ""
        right = f" {status} ──" if status else ""

        # Longest state that fits; the status goes first when even the shortest does not.
        for candidate in (*states, states[-1]):
            style, state = candidate
            if get_cwidth(left) + get_cwidth(state) + 1 + get_cwidth(right) <= columns:
                break
        else:
            right = ""
        room = columns - get_cwidth(left) - get_cwidth(right) - 1
        while get_cwidth(state) > max(0, room):
            state = state[:-1]
        fill = max(0, room - get_cwidth(state))
        return [
            ("class:rule", left),
            (style, state + " "),
            ("class:rule", "─" * fill),
            ("class:status", right[:-2] if right else ""),
            ("class:rule", right[-2:] if right else ""),
        ]

    def _prompt_fragments(self):
        if self._menu is not None:
            label = "menu > "
        elif self._choice is not None:
            label = "answer > "
        elif self._text is not None:
            label = "key > " if self._text.secret else "answer > "
        elif self._question is None:
            label = "> "
        else:
            label = self._question.prompt()
        return [("class:input-prompt", label)]

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #

    def _accept(self, buffer: Buffer) -> bool:
        """Enter pressed. Returns True to keep the text in the input line."""
        text = buffer.text.strip()
        if self._menu is not None or self._choice is not None or self._text is not None:
            return True
        if self._question is not None:
            answer = self._question.answer(text)
            if answer is None:  # a tool's question needs an explicit answer
                self._flash = ("type y (yes), n (no) or a (always)", time.monotonic() + FLASH_SECONDS)
                return False
            self._question.resolve(answer)
            return False
        if not text:
            return False
        if self._busy:
            self._queue.append(text)  # sent once the agent is done
            self._flash = (f"queued: sent when the agent is done ({len(self._queue)} waiting)", time.monotonic() + FLASH_SECONDS)
            return False
        self._spawn(self._dispatch(text))
        return False

    async def _dispatch(self, text: str) -> None:
        """Run what was typed: a /command, a !command, or a message for the agent."""
        if text.startswith("/"):
            await self._command(text)
        elif text.startswith("!") and self._bang is not None:
            await self._command(text, handler=self._bang)
        else:
            await self._run_turn(text)

    def _send_queued(self) -> None:
        """The agent is done: send the oldest queued message, if any."""
        if self._queue and not self._busy:
            self._spawn(self._dispatch(self._queue.pop(0)))

    def _interrupt(self) -> None:
        if self._menu is not None:
            self._menu.resolve(None)
            return
        if self._choice is not None:
            self._choice.resolve(None)
        if self._text is not None:
            self._buffer.reset()
            self._text.resolve(None)
        if self._question is not None:
            self._question.resolve(self._question.refuse())
        if self._busy and (self._view is not None or self._stoppable):  # an agent turn, a !command
            self._cancel.set()
            if self._queue:
                self._flash = (f"stopped: {len(self._queue)} queued message(s) dropped", time.monotonic() + FLASH_SECONDS)
            self._queue.clear()
            self._app.invalidate()
        elif not self._busy and self._buffer.text:
            self._buffer.reset()

    @property
    def stop_requested(self) -> bool:
        """Did the user press Ctrl+C during what runs now?"""
        return self._cancel.is_set()

    def toggle_details(self) -> bool:
        """Show or hide what the tool lines hide, from now on. Returns the new state."""
        self._details = not self._details
        if self._printer is not None:
            self._printer.details = self._details
        message = "tool details shown (Ctrl+O hides them)" if self._details else "tool details hidden (Ctrl+O shows them)"
        self._flash = (message, time.monotonic() + FLASH_SECONDS)
        self._app.invalidate()
        return self._details

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: "asyncio.Future") -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._app.exit(exception=task.exception())  # a bug must not vanish silently

    async def _busy_while(self, coroutine, label: str) -> None:
        self._busy, self._busy_label = True, label
        try:
            await coroutine
        finally:
            self._busy = False

    # ------------------------------------------------------------------ #
    # Output above the input line
    # ------------------------------------------------------------------ #

    def _capture(self, *renderables: RenderableType) -> str:
        """Render to a string (width-aware) so that rows can be counted."""
        with self.console.capture() as captured:
            for renderable in renderables:
                self.console.print(renderable)
        return captured.get()

    async def _print_above(self, render: Callable[[], str], pad_before: bool = False) -> None:
        """Print `render()` above the UI, which is erased and redrawn around it.

        The text takes the place of filler rows (see the module docstring),
        which keeps the input on the last row. With `pad_before`, blank rows
        go before it instead, to keep a command's output next to the input.
        """

        def work() -> None:
            text = render()
            if not text:
                return
            rows = text.count("\n")
            if pad_before:
                text = "\n" * max(0, self._peak - rows) + text
                self._peak = 0
            else:
                self._peak = max(0, self._peak - rows)
            self.console.file.write(text)
            self.console.file.flush()

        await run_in_terminal(work)

    def notify(self, text: str) -> None:
        """Print `text` (already rendered) above the input line, whatever the screen is doing.
        Safe from any thread; before the screen runs the text waits for it."""
        with self._notices_lock:
            loop = self._loop
            if loop is None:
                self._notices.append(text)
                return
        try:
            asyncio.run_coroutine_threadsafe(self._print_notice(text), loop)
        except RuntimeError:  # the loop is closed: the console is shutting down
            pass

    async def _print_notice(self, text: str) -> None:
        while not self._app.is_running:  # notices that waited for the screen
            await asyncio.sleep(0.02)
        await self._print_above(lambda: text)

    def _reset_screen(self) -> None:
        """Clear the terminal, show the banner and push the input to the bottom."""
        self.console.clear()
        text = self._capture(self.banner) if self.banner is not None else ""
        pad = max(0, self._size().rows - text.count("\n") - UI_ROWS)
        self.console.file.write(text + "\n" * pad)
        self.console.file.flush()
        self._peak = 0

    async def _open_transcript(self) -> None:
        """Show the conversation in a full-screen viewer until the user closes it."""
        from .transcript import TranscriptViewer

        turns = self._transcript()
        if not turns:
            await self._print_above(lambda: self.commands.renderer.text("Nothing to show yet.", "dim"), pad_before=True)
            return
        self.viewer = TranscriptViewer(turns, details=self._details, input=self._app.input, output=self._app.output)
        try:
            async with in_terminal():
                await self.viewer.run_async()
        finally:
            self.viewer = None

    # ------------------------------------------------------------------ #
    # Commands and turns (event loop)
    # ------------------------------------------------------------------ #

    async def _command(self, text: str, handler: Optional[Callable[[str], CommandResult]] = None) -> None:
        """Run a /command (or, with `handler`, `handler(text without its first character)`) in a worker
        thread, then show what it printed."""
        assert self._loop is not None
        name = text.split()[0] if handler is None else "!"
        self._busy, self._busy_label = True, f"running {name}"
        self._cancel = threading.Event()
        self._stoppable = handler is not None
        self._app.invalidate()
        prompt: Optional[str] = None
        try:
            try:
                if handler is None:
                    result = await self._loop.run_in_executor(None, self.commands.run, text)
                else:
                    result = await self._loop.run_in_executor(None, handler, text[1:].strip())
            except Exception as error:  # a command must never break the screen
                message = self.commands.renderer.text(f"{name}: {type(error).__name__}: {error}", "red")
                result = CommandResult(text=message)

            if result.text:
                await self._print_above(lambda: result.text, pad_before=True)
            if result.clear_screen:
                await run_in_terminal(self._reset_screen)
            if result.transcript:
                await self._open_transcript()
            if result.quit:
                self._app.exit()
                return
            prompt = result.prompt
        finally:
            self._busy = self._stoppable = False
        if prompt:
            await self._run_turn(prompt)
        else:
            self._send_queued()

    async def _stream(self, view: TurnView, printer: TurnPrinter, stop: asyncio.Event) -> None:
        """While the turn runs, print what became final."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), STREAM_PERIOD)
            except asyncio.TimeoutError:
                pass
            if not stop.is_set() and printer.has_news(view):
                await self._print_above(lambda: self._capture(*printer.take(view)))

    def _finish(self, view: TurnView, printer: TurnPrinter) -> str:
        """Called once the UI is erased: drop the live area, return the rest of the turn."""
        self._view = self._printer = None
        return self._capture(*printer.take(view, final=True))

    async def _run_turn(self, text: str) -> None:
        assert self._loop is not None
        view = TurnView(text)
        printer = TurnPrinter(self._details)
        self._cancel = threading.Event()
        self._view, self._printer = view, printer
        self._busy = True
        self._app.invalidate()
        stop = asyncio.Event()
        streamer = asyncio.ensure_future(self._stream(view, printer, stop))
        try:
            view.stats = await self._loop.run_in_executor(None, self._turn_runner, view, self._cancel)
        except Exception as error:  # the runner should not raise, but never hang the UI
            view.add_note(f"Agent error: {error}", STYLE_ERROR)
        finally:
            stop.set()
            try:
                await streamer
            finally:
                try:
                    await self._print_above(lambda: self._finish(view, printer))
                finally:
                    self._finished.append(view)
                    self._busy = False
                    self._view = self._printer = None
                    self._send_queued()

    # ------------------------------------------------------------------ #
    # Questions (called from the agent's worker thread)
    # ------------------------------------------------------------------ #

    def _call(self, coroutine):
        if self._loop is None:
            coroutine.close()
            raise RuntimeError("The screen is not running.")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def ask_permission(self, info: str, default: bool = True) -> bool:
        """A yes/no confirmation: block the calling (non-UI) thread until the user
        answers. An empty answer means `default`."""
        return self._call(self._ask(Question(info, default=default)))

    def ask_tool_permission(self, info: str, rule: Optional[str] = None, *, project: bool = False) -> Decision:
        """A tool's permission question: y, n (with a reason), or "always" for `rule`
        (`a`: this session; `p`, when `project`: in this project). An empty answer
        answers nothing. Blocks the calling (non-UI) thread."""
        return self._call(self._ask(Question(info, tool=True, rule=rule, project=project)))

    async def _ask(self, question: Question):
        question.future = asyncio.get_running_loop().create_future()
        draft = self._buffer.document  # what the user was typing: put aside, not taken as the answer
        self._buffer.reset()
        self._question = question
        self._app.invalidate()
        try:
            return await question.future
        finally:
            self._question = None
            self._buffer.reset(document=draft)
            self._app.invalidate()

    def ask_choice(
        self,
        question: str,
        options: List[Choice],
        multiple: bool = False,
        allow_other: bool = True,
        initial: int = 0,
    ) -> Optional[Answer]:
        """Ask the user to pick an option (or several, with `multiple`), or to type
        another answer (with `allow_other`); the cursor starts on option `initial`.
        Blocks the calling (non-UI) thread; None when the user skips the question."""
        if not options and not allow_other:
            raise ValueError("A question needs options, or must accept a typed answer.")
        return self._call(self._ask_choice(question, list(options), multiple, allow_other, initial))

    async def _ask_choice(
        self, question: str, options: List[Choice], multiple: bool, allow_other: bool, initial: int = 0
    ) -> Optional[Answer]:
        choice = ChoiceQuestion(question, options, multiple, allow_other, asyncio.get_running_loop().create_future())
        draft = self._buffer.document  # what the user was typing: kept for later
        self._buffer.reset()
        self._choice = choice
        choice.cursor = max(0, min(initial, len(options) - 1))
        if not options:
            choice.cursor = choice.other_row
        self._app.invalidate()
        try:
            return await choice.future
        finally:
            self._choice = None
            self._buffer.reset(document=draft)
            self._app.invalidate()

    def ask_text(self, info: str, secret: bool = False) -> Optional[str]:
        """Ask for a line of text (masked, and kept out of the history, when `secret`).
        Blocks the calling (non-UI) thread; None when the user cancels (Esc, Ctrl+C)."""
        return self._call(self._ask_text(info, secret))

    async def _ask_text(self, info: str, secret: bool) -> Optional[str]:
        question = TextQuestion(info, asyncio.get_running_loop().create_future(), secret)
        draft = self._buffer.document
        self._buffer.reset()
        self._text = question
        self._app.invalidate()
        try:
            return await question.future
        finally:
            self._text = None
            self._buffer.reset(document=draft)
            self._app.invalidate()

    # ------------------------------------------------------------------ #
    # Menus (called from a command's worker thread)
    # ------------------------------------------------------------------ #

    def ask_menu(self, title: str, items: List[MenuItem]) -> Optional[Dict[str, bool]]:
        """Show a checklist and block the calling (non-UI) thread until the user
        applies it (``{key: checked}``) or cancels it (None)."""
        return self._call(self._choose(title, items))

    async def _choose(self, title: str, items: List[MenuItem]) -> Optional[Dict[str, bool]]:
        menu = Menu(title, [replace(item) for item in items], asyncio.get_running_loop().create_future())
        self._menu = menu
        self._app.invalidate()
        try:
            return await menu.future
        finally:
            self._menu = None
            self._app.invalidate()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def run_async(self) -> None:
        self._reset_screen()
        with self._notices_lock:  # from here on `notify` goes straight to the loop
            self._loop = asyncio.get_running_loop()
            waiting, self._notices = self._notices, []
        for text in waiting:
            self._spawn(self._print_notice(text))
        await self._app.run_async()

    def run(self) -> None:
        asyncio.run(self.run_async())
