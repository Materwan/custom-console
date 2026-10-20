"""The prompt box: Enter sends, Shift+Enter or Ctrl+J adds a line, Up and Down walk the history, Tab completes."""

from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

MIN_HEIGHT, MAX_HEIGHT = 3, 12


class PromptBox(TextArea):
    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class CompleteRequested(Message):
        """Tab was pressed; the app asks the completion engine and shows the popup."""

    class PasteRequested(Message):
        """Ctrl+V: the app looks at the clipboard (an image, or text)."""

    class PopupKey(Message):
        """A key meant for the completion popup while it is open."""

        def __init__(self, key: str):
            super().__init__()
            self.key = key

    BINDINGS = [
        Binding("ctrl+v", "paste_clipboard", "Paste", show=False, priority=True),
        Binding("ctrl+x", "drop_chip", "Remove attachment", show=False, priority=True),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__("", soft_wrap=True, tab_behavior="focus", show_line_numbers=False, id="prompt",
                         placeholder="Message Clara...   (/help for commands, @file to attach, !cmd to run)", **kwargs)  # type: ignore[arg-type]
        self.entries: list[str] = []  # the history of what was sent, oldest first
        self._position: int | None = None  # where in the history we are; None: not walking
        self._draft = ""
        self.popup_open = False

    # ------------------------------------------------------------------ history
    def remember(self, entries: list[str]) -> None:
        self.entries = entries
        self._position = None

    def _show(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def _walk(self, step: int) -> bool:
        if not self.entries:
            return False
        if self._position is None:
            if step > 0:
                return False
            self._draft = self.text
            self._position = len(self.entries)
        target = self._position + step
        if target < 0:
            return True
        if target >= len(self.entries):
            self._position = None
            self._show(self._draft)
            return True
        self._position = target
        self._show(self.entries[target])
        return True

    # ------------------------------------------------------------------ keys
    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if self.popup_open and key in ("up", "down", "tab", "enter", "escape"):
            event.stop()
            event.prevent_default()
            self.post_message(self.PopupKey(key))
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text
            if text.endswith("\\") and "\n" not in text[-2:]:  # a trailing backslash continues the line
                self.load_text(text[:-1] + "\n")
                self.move_cursor(self.document.end)
                return
            self.post_message(self.Submitted(text))
            return
        if key in ("shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.CompleteRequested())
            return
        row, _ = self.cursor_location
        if key == "up" and row == 0 and self._walk(-1):
            event.stop()
            event.prevent_default()
            return
        if key == "down" and row == self.document.line_count - 1 and self._position is not None and self._walk(1):
            event.stop()
            event.prevent_default()
            return
        if key not in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown") and self._position is not None \
                and event.is_printable:
            self._position = None  # editing a recalled line makes it a new message
        await super()._on_key(event)

    def action_paste_clipboard(self) -> None:
        self.post_message(self.PasteRequested())

    def action_drop_chip(self) -> None:
        self.app.action_drop_chip()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ size
    def on_text_area_changed(self) -> None:
        lines = max(self.document.line_count, 1)
        self.styles.height = max(MIN_HEIGHT, min(lines + 2, MAX_HEIGHT))

    def take(self) -> str:
        """The text, and the box emptied."""
        text = self.text
        self.load_text("")
        self._position = None
        return text
