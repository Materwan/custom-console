"""A full-screen viewer with search, for the journal and the transcript."""

from __future__ import annotations

from collections.abc import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea
from textual.widgets.text_area import Selection


class ViewerScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("ctrl+f", "search", "Search", show=False),
        Binding("n", "next", "Next match", show=False),
        Binding("N", "previous", "Previous match", show=False),
    ]

    def __init__(self, title: str, lines: Sequence[str]):
        super().__init__()
        self.title_text = title
        self.body = "\n".join(lines)
        self.needle = ""
        self.matches: list[tuple[int, int]] = []
        self.current = -1

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, id="viewer-title")
        yield TextArea(self.body, read_only=True, soft_wrap=True, show_line_numbers=False, id="viewer-text")
        yield Input(placeholder="search (Enter: find, Esc: back)", id="viewer-search")
        yield Static("Esc closes   / searches   n next   N previous   arrows and PageUp/PageDown scroll", id="viewer-help")

    def on_mount(self) -> None:
        self.query_one("#viewer-text", TextArea).focus()

    def action_close(self) -> None:
        search = self.query_one("#viewer-search", Input)
        if search.has_class("open"):
            self.close_search()
        else:
            self.dismiss(None)

    def action_search(self) -> None:
        search = self.query_one("#viewer-search", Input)
        search.add_class("open")
        search.focus()

    def close_search(self) -> None:
        self.query_one("#viewer-search", Input).remove_class("open")
        self.query_one("#viewer-text", TextArea).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.find(event.value)
        self.close_search()

    def find(self, needle: str) -> None:
        self.needle = needle
        self.matches = []
        self.current = -1
        if not needle:
            return
        folded = needle.lower()
        for row, line in enumerate(self.body.split("\n")):
            column = line.lower().find(folded)
            while column != -1:
                self.matches.append((row, column))
                column = line.lower().find(folded, column + len(folded))
        self.go(1)

    def go(self, step: int) -> None:
        if not self.matches:
            self.notify(f"No match for {self.needle!r}" if self.needle else "Nothing to find", timeout=2)
            return
        self.current = (self.current + step) % len(self.matches)
        row, column = self.matches[self.current]
        text = self.query_one("#viewer-text", TextArea)
        text.selection = Selection((row, column), (row, column + len(self.needle)))
        text.scroll_cursor_visible()
        self.notify(f"{self.current + 1} of {len(self.matches)}", timeout=1)

    def action_next(self) -> None:
        self.go(1)

    def action_previous(self) -> None:
        self.go(-1)
