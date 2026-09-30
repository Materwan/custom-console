"""Rendering of a turn: live plain text while streaming, Markdown at the end."""

from __future__ import annotations

from typing import List, Sequence, Tuple

from prompt_toolkit.utils import get_cwidth
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from .turn import NOTE, STYLE_ERROR, STYLE_NOTE, STYLE_PERMISSION, STYLE_TOOL, TurnStats, Segment, TurnView

StyledLine = Tuple[str, str]  # (prompt_toolkit style, text)

PROMPT_MARK = "❯ "
ELLIPSIS = "…"

# Final (rich) style of each note kind.
RICH_NOTE_STYLES = {
    STYLE_NOTE: "dim",
    STYLE_TOOL: "dim cyan",
    STYLE_PERMISSION: "yellow",
    STYLE_ERROR: "red",
}


# --------------------------------------------------------------------------- #
# Live (plain text) rendering
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


def live_lines(view: TurnView, width: int, max_rows: int) -> List[StyledLine]:
    """Styled lines of the streamed output, limited to its last `max_rows` rows.

    The user's message comes first, then the model text as plain text and the
    activity notes (tool calls, permissions) in their own style.
    """
    lines: List[StyledLine] = []
    for piece in wrap_line(PROMPT_MARK + view.prompt, width):
        lines.append(("class:prompt", piece))

    for segment in view.snapshot():
        if segment.kind == NOTE:
            for piece in wrap_line(segment.text, width):
                lines.append((f"class:{segment.style}", piece))
        else:
            for logical in segment.text.split("\n"):
                for piece in wrap_line(logical, width):
                    lines.append(("", piece))

    if max_rows > 0 and len(lines) > max_rows:
        lines = [("class:note", ELLIPSIS), *lines[-(max_rows - 1) :]] if max_rows > 1 else lines[-1:]
    return lines


# --------------------------------------------------------------------------- #
# Final (Markdown) rendering
# --------------------------------------------------------------------------- #


def stats_line(stats: TurnStats) -> str:
    return (
        f"{stats.input_tokens} prompt + {stats.output_tokens} completion = "
        f"{stats.total_tokens} tokens · {stats.duration:.1f}s · "
        f"{stats.tokens_per_second:.1f} tokens/s"
    )


def turn_renderables(view: TurnView) -> List[RenderableType]:
    """What replaces the streamed text: the user's message, the activity lines
    and the answer rendered as Markdown, then the statistics."""
    parts: List[RenderableType] = [Text(PROMPT_MARK + view.prompt, style="bold cyan"), Text("")]

    def flush(notes: Sequence[Segment]) -> None:
        for note in notes:
            parts.append(Text(note.text, style=RICH_NOTE_STYLES.get(note.style, "dim")))

    pending_notes: List[Segment] = []
    for segment in view.snapshot():
        if segment.kind == NOTE:
            pending_notes.append(segment)
            continue
        if not segment.text.strip():
            continue
        if pending_notes:
            flush(pending_notes)
            pending_notes = []
            parts.append(Text(""))
        parts.append(Markdown(segment.text.strip()))
        parts.append(Text(""))
    if pending_notes:
        flush(pending_notes)
        parts.append(Text(""))

    if view.stats is not None:
        parts.append(Text(stats_line(view.stats), style="dim italic"))
        parts.append(Text(""))
    return parts


def render_turn(view: TurnView) -> Group:
    return Group(*turn_renderables(view))
