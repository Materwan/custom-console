"""Rendering of a turn.

While the agent works, what is final — finished paragraphs, finished tool
lines — goes to the terminal's scrollback as it comes, rendered as Markdown,
so the terminal can be scrolled during the answer. Only the part that still
changes stays in the live area above the input, as plain text.
:class:`TurnPrinter` remembers how far a turn was printed.

Tool lines are one line each; what they hide (a diff, a command's output) is
shown when the details are on (Ctrl+O, or the transcript viewer).
"""

from __future__ import annotations

import copy
import re
from typing import List, Optional, Tuple

from prompt_toolkit.utils import get_cwidth
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from .turn import DIFF, NOTE, STYLE_ERROR, STYLE_NOTE, STYLE_PERMISSION, STYLE_TOOL, TEXT, Segment, TurnStats, TurnView

StyledLine = Tuple[str, str]  # (prompt_toolkit style, text)

PROMPT_MARK = "❯ "
ELLIPSIS = "…"
DIFF_INDENT = "  "
DETAIL_INDENT = "    "

# Final (rich) style of each note kind.
RICH_NOTE_STYLES = {
    STYLE_NOTE: "dim",
    STYLE_TOOL: "dim cyan",
    STYLE_PERMISSION: "yellow",
    STYLE_ERROR: "red",
}

# Final (rich) style of a diff line or a checklist line, chosen by its first character.
RICH_DIFF_STYLES = {"+": "green", "-": "red", "@": "cyan", "…": "dim italic"}
RICH_TODO_STYLES = {"☑": "dim", "◐": "bold yellow"}


def diff_style(line: str) -> str:
    """prompt_toolkit style class of one line of a diff."""
    return {"+": "class:diff-add", "-": "class:diff-del", "@": "class:diff-hunk", "…": "class:note"}.get(
        line[:1], "class:diff-ctx"
    )


def todo_style(line: str) -> str:
    return {"☑": "class:todo-done", "◐": "class:todo-active"}.get(line[:1], "class:todo")


# --------------------------------------------------------------------------- #
# Wrapping
# --------------------------------------------------------------------------- #


def wrap_line(line: str, width: int) -> List[str]:
    """Wrap one logical line to `width` terminal cells.

    Breaks at the last space when possible, otherwise in the middle of a word.
    Wide (CJK/emoji) characters count for their display width.
    """
    line = line.replace("\r", "").replace("\t", "    ")
    if width < 1:
        return [line]

    lines: List[str] = []
    buffer = ""
    used = 0
    last_space = -1
    for char in line:
        char_width = get_cwidth(char)
        if used + char_width > width:
            if char == " ":  # the overflowing space is itself the break point
                lines.append(buffer)
                buffer, used, last_space = "", 0, -1
                continue
            if last_space > 0:
                lines.append(buffer[:last_space])
                buffer = buffer[last_space + 1 :]
                used = sum(get_cwidth(c) for c in buffer)
            else:
                lines.append(buffer)
                buffer, used = "", 0
            last_space = -1
        if char == " ":
            last_space = len(buffer)
        buffer += char
        used += char_width
    lines.append(buffer)
    return lines


# --------------------------------------------------------------------------- #
# Segments other than model text
# --------------------------------------------------------------------------- #


def _styled_block(text: str, width: int, style_of, indent: str) -> List[StyledLine]:
    lines: List[StyledLine] = []
    for logical in text.split("\n"):
        for piece in wrap_line(indent + logical, width):
            lines.append((style_of(logical), piece))
    return lines


def detail_lines(segment: Segment, width: int) -> List[StyledLine]:
    """What a tool line hides, indented under it."""
    if not segment.detail:
        return []
    style_of = diff_style if segment.detail_kind == DIFF else (lambda _line: "class:detail")
    return _styled_block(segment.detail, width, style_of, DETAIL_INDENT)


def segment_lines(segment: Segment, width: int, details: bool = False) -> List[StyledLine]:
    """Styled, wrapped lines of a segment that is not model text."""
    if segment.kind == DIFF:  # a diff saved by an older version: a detail of the line above
        return _styled_block(segment.text, width, diff_style, DIFF_INDENT) if details else []
    lines = [(f"class:{segment.style}", piece) for piece in wrap_line(segment.text, width)]
    if details:
        lines.extend(detail_lines(segment, width))
    return lines


def todo_lines(checklist: str, width: int) -> List[StyledLine]:
    return _styled_block(checklist, width, todo_style, "") if checklist.strip() else []


def _rich_block(text: str, styles: dict, default: str, indent: str) -> Text:
    rendered = Text(no_wrap=False)
    for index, line in enumerate(text.split("\n")):
        if index:
            rendered.append("\n")
        rendered.append(indent + line, style=styles.get(line[:1], default))
    return rendered


def segment_renderable(segment: Segment, details: bool = False) -> Optional[RenderableType]:
    """Final rendering of a segment that is not model text (None: nothing to show)."""
    if segment.kind == DIFF:
        return _rich_block(segment.text, RICH_DIFF_STYLES, "dim", DIFF_INDENT) if details else None
    line = Text(segment.text, style=RICH_NOTE_STYLES.get(segment.style, "dim"))
    if not (details and segment.detail):
        return line
    if segment.detail_kind == DIFF:
        hidden = _rich_block(segment.detail, RICH_DIFF_STYLES, "dim", DETAIL_INDENT)
    else:
        hidden = _rich_block(segment.detail, {}, "dim", DETAIL_INDENT)
    return Group(line, hidden)


def todo_renderable(checklist: str) -> Text:
    return _rich_block(checklist, RICH_TODO_STYLES, "", "")


# --------------------------------------------------------------------------- #
# What part of the streamed Markdown is final
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CONTINUES = re.compile(r"^(\s|[-*+](\s|$)|\d+[.)](\s|$))")  # indented, or a list item
_LOOKAHEAD = 4  # characters of a line needed to tell a list item from a paragraph


def markdown_cut(text: str) -> int:
    """Length of the start of `text` made of finished Markdown blocks (0: none yet).

    A block is finished once a blank line follows it and the next block has
    started, outside code fences. A cut is never made before an indented line
    or a list item: they may continue the block above (lists, code).
    """
    cut = 0
    fence: Optional[str] = None
    position = 0
    previous_blank = False
    lines = text.split("\n")
    for index, line in enumerate(lines):
        writing = index == len(lines) - 1  # the line still being written
        if fence is None and previous_blank and line.strip() and (not writing or len(line) >= _LOOKAHEAD):
            if not _CONTINUES.match(line):
                cut = position
        if writing:
            break
        match = _FENCE.match(line)
        if match is not None:
            mark = match.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip().strip(mark[0]):
                fence = None
        previous_blank = fence is None and not line.strip()
        position += len(line) + 1
    return cut


# --------------------------------------------------------------------------- #
# Printing a turn as it comes
# --------------------------------------------------------------------------- #


def stats_line(stats: TurnStats) -> str:
    line = (
        f"{stats.input_tokens} prompt + {stats.output_tokens} completion = "
        f"{stats.total_tokens} tokens · {stats.duration:.1f}s · "
        f"{stats.tokens_per_second:.1f} tokens/s"
    )
    if stats.context_percent is not None:
        line += f" · context {stats.context_percent:.0f}%"
    return line


class TurnPrinter:
    """How far a turn was printed to the scrollback, and what can be printed next.

    Spacing: a blank line around Markdown blocks and the checklist, and after
    the user's message; consecutive activity lines stay together.
    """

    def __init__(self, details: bool = False):
        self.details = details  # tool lines are printed with what they hide
        self.prompt_done = False
        self.index = 0  # first segment not entirely printed
        self.offset = 0  # characters of that segment already printed (model text)
        self._last = ""  # what was printed last: "", "prompt", "block" or "line"

    def _gap(self, parts: List[RenderableType], kind: str) -> None:
        if self._last in ("prompt", "block") or (kind == "block" and self._last == "line"):
            parts.append(Text(""))
        self._last = kind

    def take(self, view: TurnView, final: bool = False) -> List[RenderableType]:
        """What became final since the last call (everything left, with `final`)."""
        parts: List[RenderableType] = []
        if not self.prompt_done:
            parts.append(Text(PROMPT_MARK + view.prompt, style="bold cyan"))
            self.prompt_done, self._last = True, "prompt"

        segments = view.snapshot()
        while self.index < len(segments):
            segment = segments[self.index]
            still_growing = self.index == len(segments) - 1 and not final
            if segment.kind == TEXT:
                rest = segment.text[self.offset :]
                cut = markdown_cut(rest) if still_growing else len(rest)
                if rest[:cut].strip():
                    self._gap(parts, "block")
                    parts.append(Markdown(rest[:cut].strip()))
                self.offset += cut
                if still_growing:
                    break  # more text may come in this segment
            else:
                if not segment.done and not final:
                    break  # a running tool: its line still changes, and so does what follows
                renderable = segment_renderable(segment, self.details)
                if renderable is not None:
                    self._gap(parts, "line")
                    parts.append(renderable)
            self.index, self.offset = self.index + 1, 0

        if final:
            if view.todos.strip():
                self._gap(parts, "block")
                parts.append(todo_renderable(view.todos))
            parts.append(Text(""))
            if view.stats is not None:
                parts.append(Text(stats_line(view.stats), style="dim italic"))
                parts.append(Text(""))
        return parts

    def has_news(self, view: TurnView) -> bool:
        """Would :meth:`take` print something now? (Does not move the position.)"""
        return bool(copy.copy(self).take(view))


def live_lines(view: TurnView, width: int, max_rows: int, printer: Optional[TurnPrinter] = None) -> List[StyledLine]:
    """Styled lines of what is not printed yet, limited to its last `max_rows` rows.

    The user's message comes first (until it is printed), then the model text
    as plain text and the activity lines in their own style.
    """
    lines: List[StyledLine] = []
    if printer is None or not printer.prompt_done:
        for piece in wrap_line(PROMPT_MARK + view.prompt, width):
            lines.append(("class:prompt", piece))

    start = printer.index if printer is not None else 0
    details = printer.details if printer is not None else False
    for index, segment in enumerate(view.snapshot()[start:], start):
        if segment.kind == TEXT:
            text = segment.text[printer.offset :] if printer is not None and index == start else segment.text
            for logical in text.split("\n"):
                for piece in wrap_line(logical, width):
                    lines.append(("", piece))
        else:
            lines.extend(segment_lines(segment, width, details))

    if max_rows > 0 and len(lines) > max_rows:
        lines = [("class:note", ELLIPSIS), *lines[-(max_rows - 1) :]] if max_rows > 1 else lines[-1:]
    return lines


# --------------------------------------------------------------------------- #
# A whole turn at once
# --------------------------------------------------------------------------- #


def turn_renderables(view: TurnView, details: bool = False) -> List[RenderableType]:
    """The user's message, the activity lines and the answer rendered as
    Markdown, then the checklist and the statistics."""
    return TurnPrinter(details).take(view, final=True)


def render_turn(view: TurnView, details: bool = False) -> Group:
    return Group(*turn_renderables(view, details))
