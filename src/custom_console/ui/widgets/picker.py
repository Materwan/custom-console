"""A small modal list to choose one thing from, by mouse click or by keyboard (/model uses it)."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class PickerScreen(ModalScreen[str | None]):
    """Dismissed with the chosen row's value, or None on Esc or a click outside the list."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, rows: Sequence[tuple[str, str]], current: str = ""):
        super().__init__()
        self.title_text = title
        self.rows = list(rows)
        self.current = current

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, id="picker-title")
        yield OptionList(*[Option(Text(("* " if value == self.current else "  ") + label), id=str(i))
                           for i, (value, label) in enumerate(self.rows)], id="picker-list")
        yield Static("Click or Enter chooses   Esc cancels   arrows move", id="picker-help")

    def on_mount(self) -> None:
        options = self.query_one("#picker-list", OptionList)
        options.highlighted = next((i for i, (value, _) in enumerate(self.rows) if value == self.current), 0)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()  # the main screen's completion popup listens for this message too
        self.dismiss(self.rows[int(event.option.id or 0)][0])

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:  # the dimmed area around the box
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
