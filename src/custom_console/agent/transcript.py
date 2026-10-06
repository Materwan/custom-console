"""Full-screen viewer of the conversation (Ctrl+T or /transcript).

The turns are shown as they were printed, but each tool line can be unfolded to
show what it hides (the diff of a file change, a command's output...): Enter on
the selected line, or a click on any of them. Tab and Shift+Tab select the next
and previous tool line; the arrows, PageUp/PageDown (or Space) and the mouse
wheel scroll. q or Escape goes back to the conversation.

It runs as a separate full-screen application, on the terminal's alternate
screen, so the conversation's scrollback is left as it was.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown

from .render import PROMPT_MARK, StyledLine, detail_lines, diff_style, stats_line, todo_lines, wrap_line
from .turn import DIFF, TEXT, TurnView

Row = List[Tuple[str, str]]  # the fragments of one screen row
GUTTER = 2  # columns on the left for the fold mark
WHEEL_ROWS = 3

HELP = "Tab/Shift+Tab tool lines · Enter or click: unfold · a: all · ↑↓ PgUp PgDn: scroll · q: back"


@dataclass
class _Item:
    rows: List[Row]  # what is always shown
    hidden: List[Row] = field(default_factory=list)  # shown when the item is unfolded
    open: bool = False

    @property
    def foldable(self) -> bool:
        return bool(self.hidden)

    def shown(self) -> List[Row]:
        return self.rows + self.hidden if self.open else self.rows


def _styled_rows(lines: Sequence[StyledLine]) -> List[Row]:
    return [[(style, text)] for style, text in lines]


def _markdown_rows(text: str, width: int) -> List[Row]:
    console = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor", width=width, legacy_windows=False)
    console.print(Markdown(text.strip()))
    rows = [list(row) for row in split_lines(to_formatted_text(ANSI(console.file.getvalue())))]  # type: ignore[attr-defined]
    while rows and not "".join(text for _, text, *_ in rows[-1]).strip():
        rows.pop()
    return [[(style, text) for style, text, *_ in row] for row in rows]


def build_items(turns: Sequence[TurnView], width: int) -> List[_Item]:
    """The turns as items: rule, prompt, answer blocks, tool lines (foldable)..."""
    inner = max(10, width - GUTTER)
    items: List[_Item] = []
    for number, view in enumerate(turns, start=1):
        items.append(_Item([[("class:rule", f"── {number} " + "─" * max(0, inner - len(str(number)) - 4))]]))
        items.append(_Item(_styled_rows([("class:prompt", piece) for piece in wrap_line(PROMPT_MARK + view.prompt, inner)])))
        for segment in view.snapshot():
            if segment.kind == TEXT:
                if segment.text.strip():
                    items.append(_Item(_markdown_rows(segment.text, inner)))
            elif segment.kind == DIFF:  # older sessions: the diff of the tool line above
                rows = _styled_rows([(diff_style(line), "  " + line) for line in segment.text.split("\n")])
                if items and items[-1].rows and not items[-1].hidden:
                    items[-1].hidden = rows
                else:
                    items.append(_Item([[("class:detail", "(diff)")]], rows))
            else:
                line = _styled_rows([(f"class:{segment.style}", piece) for piece in wrap_line(segment.text, inner)])
                items.append(_Item(line, _styled_rows(detail_lines(segment, inner))))
        if view.todos.strip():
            items.append(_Item(_styled_rows(todo_lines(view.todos, inner))))
        if view.stats is not None:
            items.append(_Item([[("class:note", stats_line(view.stats))]]))
        items.append(_Item([[]]))
    return items


class TranscriptViewer:
    def __init__(
        self,
        turns: Sequence[TurnView],
        *,
        details: bool = False,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
    ) -> None:
        from .ui import STYLE  # the same colours as the conversation

        self.turns = list(turns)
        self._details = details
        self._items: List[_Item] = []
        self._built_for = -1  # width the items were built for
        self._scroll = 10**9  # starts at the end: the latest turn
        self._selected: Optional[int] = None
        self.app: Application = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(FormattedTextControl(self._body_fragments)),
                        Window(FormattedTextControl(self._footer_fragments), height=1),
                    ]
                )
            ),
            key_bindings=self._keys(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
        )

    # -- content ------------------------------------------------------------------------ #

    def _width(self) -> int:
        return max(GUTTER + 10, self.app.output.get_size().columns - 1)

    def _height(self) -> int:
        return max(1, self.app.output.get_size().rows - 1)

    def items(self) -> List[_Item]:
        width = self._width()
        if width != self._built_for:
            opened = [item.open for item in self._items]
            self._items = build_items(self.turns, width)
            for item, was_open in zip(self._items, opened or [self._details] * len(self._items)):
                item.open = was_open and item.foldable
            self._built_for = width
            if self._selected is None:
                foldable = self._foldable()
                self._selected = foldable[-1] if foldable else None
        return self._items

    def _foldable(self) -> List[int]:
        return [index for index, item in enumerate(self._items) if item.foldable]

    def _rows(self) -> List[Tuple[int, int, Row]]:
        """Every row: (item index, row index within the item, fragments)."""
        return [(index, number, row) for index, item in enumerate(self.items()) for number, row in enumerate(item.shown())]

    def _clamp(self, total: int) -> None:
        self._scroll = max(0, min(self._scroll, total - self._height()))

    # -- actions ------------------------------------------------------------------------- #

    def scroll_by(self, delta: int) -> None:
        self._scroll = max(0, min(self._scroll, len(self._rows()) - self._height())) + delta
        self.app.invalidate()

    def toggle(self, index: Optional[int] = None) -> None:
        index = self._selected if index is None else index
        items = self.items()
        if index is not None and items[index].foldable:
            items[index].open = not items[index].open
            self._selected = index
            self._reveal(index)
        self.app.invalidate()

    def toggle_all(self) -> None:
        items = self.items()
        state = not all(items[index].open for index in self._foldable())
        for index in self._foldable():
            items[index].open = state
        if self._selected is not None:
            self._reveal(self._selected)
        self.app.invalidate()

    def select(self, step: int) -> None:
        """Select the next (`step` > 0) or previous tool line and bring it into view."""
        self.items()
        foldable = self._foldable()
        if not foldable:
            return
        if self._selected is None:
            self._selected = foldable[0 if step > 0 else -1]
        else:
            after = [i for i in foldable if (i > self._selected if step > 0 else i < self._selected)]
            if after:
                self._selected = after[0] if step > 0 else after[-1]
        self._reveal(self._selected)
        self.app.invalidate()

    def _reveal(self, index: int) -> None:
        """Scroll so that the item is visible (its first row at least)."""
        rows = self._rows()
        first = next(n for n, (item, _, _) in enumerate(rows) if item == index)
        size = len(self._items[index].shown())
        height = self._height()
        self._clamp(len(rows))
        if first < self._scroll:
            self._scroll = first
        elif first + size > self._scroll + height:
            self._scroll = max(first + min(size, height) - height, 0)

    # -- drawing ------------------------------------------------------------------------- #

    def _mouse(self, index: int) -> Callable[[MouseEvent], object]:
        def handler(event: MouseEvent) -> object:
            if event.event_type == MouseEventType.SCROLL_UP:
                self.scroll_by(-WHEEL_ROWS)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self.scroll_by(WHEEL_ROWS)
            elif event.event_type == MouseEventType.MOUSE_UP and self._items[index].foldable:
                self.toggle(index)
            else:
                return NotImplemented
            return None

        return handler

    def _body_fragments(self):
        rows = self._rows()
        self._clamp(len(rows))
        width = self._width()
        fragments: list = []
        for position, (index, number, row) in enumerate(rows[self._scroll : self._scroll + self._height()]):
            if position:
                fragments.append(("", "\n"))
            item = self._items[index]
            handler = self._mouse(index)
            selected = index == self._selected and number == 0
            gutter = ("▾ " if item.open else "▸ ") if item.foldable and number == 0 else " " * GUTTER
            used = GUTTER + sum(get_cwidth(text) for _, text in row)
            line = [("class:note", gutter), *row, ("", " " * max(0, width - used))]
            for style, text in line:
                fragments.append((style + " reverse" if selected else style, text, handler))
        return fragments

    def _footer_fragments(self):
        rows = len(self._rows())
        height = self._height()
        where = f" {min(rows, self._scroll + height)}/{rows} " if rows > height else " "
        text = f" Transcript · {len(self.turns)} exchange(s) ·{where}· {HELP}"
        return [("class:menu-hint", text[: self._width()])]

    # -- keys ------------------------------------------------------------------------------ #

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()

        def bind(*names: str, eager: bool = False):
            def decorate(function):
                for name in names:
                    keys.add(name, eager=eager)(lambda event, f=function: f())
                return function

            return decorate

        bind("q", "c-c", "c-t")(lambda: self.app.exit())
        bind("escape", eager=True)(lambda: self.app.exit())
        bind("up", "k")(lambda: self.scroll_by(-1))
        bind("down", "j")(lambda: self.scroll_by(1))
        bind("pageup")(lambda: self.scroll_by(-(self._height() - 1)))
        bind("pagedown", "space")(lambda: self.scroll_by(self._height() - 1))
        bind("home", "g")(lambda: self.scroll_by(-(10**9)))
        bind("end", "G")(lambda: self.scroll_by(10**9))
        bind("tab", "n")(lambda: self.select(1))
        bind("s-tab", "p")(lambda: self.select(-1))
        bind("enter")(lambda: self.toggle())
        bind("a")(self.toggle_all)
        return keys

    async def run_async(self) -> None:
        await self.app.run_async()
