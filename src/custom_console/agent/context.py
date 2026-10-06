"""The console's side of the conversation's context.

The conversation itself lives on the Clara server, together with its summary and the
compaction that shrinks it when the context window fills up. What stays here: which
conversation this is, the project instructions file, the last size the server reported, and
an estimate of the parts the console sends with every request (system prompt, tools).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from .tools.state import StaleFile

CHARS_PER_TOKEN = 3.5  # rough average for mixed prose and code
DEFAULT_WINDOW = 32_768  # until the server tells the real one
PROJECT_FILE_LIMIT = 8_000  # characters of the project instructions file that are used

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STALE_FILES_SHOWN = 10
NOTES_START = "[Automatic note, not written by the user."


def describe_now(now: datetime) -> str:
    """``2026-10-01 14:32 (Thursday, UTC+02:00)``: no locale, no ambiguity."""
    offset = now.strftime("%z") or "+0000"
    return f"{now:%Y-%m-%d %H:%M} ({WEEKDAYS[now.weekday()]}, UTC{offset[:3]}:{offset[3:]})"


def _when(timestamp: float, now: datetime) -> str:
    moment = datetime.fromtimestamp(timestamp, tz=now.tzinfo)
    return f"at {moment:%H:%M}" if moment.date() == now.date() else f"on {moment:%Y-%m-%d} at {moment:%H:%M}"


def turn_notes(
    stale: Sequence["StaleFile"] = (),
    working_directory: str = "",
    extra: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> str:
    """What the model is told along with each message: the working directory, the files that
    changed since it read them (its earlier reads of them are outdated), then `extra` blocks
    (files the user attached, a command the user ran...). "" when there is nothing to say.

    The date and time are not here: the server gives them, in this computer's time zone (the
    request's `timezone`). `now` is only used to say when a file changed.

    It is sent as the *prefix* of the message (the server keeps it in the history but leaves it
    out of summaries) rather than put into the system prompt: the system prompt and the history
    then stay the same from one turn to the next, so the model's prompt cache is not invalidated
    every turn.

    Its form was chosen by trying it on a small local model (qwen3.5:4b): an
    XML-like ``<context>`` block before the message made it end its turn without
    an answer most of the time; a bracketed note saying it is not from the user did not.
    """
    now = now or datetime.now().astimezone()
    lines: List[str] = []
    if working_directory:
        lines.append(f"Working directory: {working_directory}.")
    if stale:
        lines.append(
            "Files changed since you read them; what you read of them is outdated, read them again "
            "before answering about them or changing them:"
        )
        for item in stale[:STALE_FILES_SHOWN]:
            what = "deleted" if item.modified is None else f"modified {_when(item.modified, now)}"
            if not item.complete:
                what += ", you had read only part of it"
            lines.append(f"- {item.path} ({what})")
        if len(stale) > STALE_FILES_SHOWN:
            lines.append(f"- … and {len(stale) - STALE_FILES_SHOWN} more")
    blocks = [f"{NOTES_START} " + "\n".join(lines) + "]"] if lines else []
    blocks.extend(block for block in extra if block.strip())
    return "\n\n".join(blocks)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


@dataclass
class Breakdown:
    window: int
    used: int
    instructions: int = 0
    project: int = 0
    summary: int = 0
    tools: int = 0
    messages: int = 0
    tool_count: int = 0

    @property
    def percent(self) -> float:
        return 100 * self.used / self.window if self.window else 0.0

    @property
    def free(self) -> int:
        return max(0, self.window - self.used)


class ContextManager:
    def __init__(
        self,
        *,
        base_session_id: str,
        window: int = DEFAULT_WINDOW,
        project_file: Optional[Path] = None,
    ) -> None:
        self.session_id = base_session_id  # the conversation, as the server knows it
        self.window = window
        self.project_file = project_file

        self.summary = ""  # the server's summary of the older messages (for /context, /compact)
        self.disabled_tools: List[str] = []  # tools the user turned off (/tools)
        self.plan_mode = False  # /plan: only the tools that read are offered
        self._instructions = 0
        self._tools = 0
        self._tool_count = 0
        self._tokens: Optional[int] = None  # size of the context at the end of the last turn

    # -- identity of the conversation ----------------------------------------- #

    def state(self) -> Dict[str, Any]:
        """What identifies the conversation, for the saved session."""
        return {"conversation": self.session_id, "summary": self.summary}

    def restore(self, state: Dict[str, Any]) -> None:
        """Go back to a conversation saved with `state()` (older sessions called it "base")."""
        self.session_id = str(state.get("conversation") or state.get("base") or self.session_id)
        self.summary = str(state.get("summary", ""))
        self._tokens = None

    def reset(self, session_id: str) -> None:
        """A new conversation (``/clear``): the old one stays on the server."""
        self.session_id = session_id
        self.summary = ""
        self._tokens = None

    # -- what goes into the request ---------------------------------------------- #

    def project_instructions(self) -> str:
        """Content of the project instructions file, read fresh (it may be edited
        between two turns)."""
        if self.project_file is None:
            return ""
        try:
            text = self.project_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return text[:PROJECT_FILE_LIMIT]

    def additional_context(self) -> str:
        """Added to the instructions sent with each request."""
        parts: List[str] = []
        project = self.project_instructions()
        if project:
            parts.append(f"## Project instructions ({self.project_file.name})\n{project}")  # type: ignore[union-attr]
        if self.plan_mode:
            parts.append(
                "## Plan mode\n"
                "The user turned plan mode on (/plan): only the tools that read are available. Explore, "
                "then answer with a plan (steps, files to change, how to check). Do not try to change "
                "anything until the user turns plan mode off."
            )
        if self.disabled_tools:
            parts.append(
                "## Tools turned off by the user\n"
                + ", ".join(self.disabled_tools)
                + "\nThese tools are not available: do not call them. If the task needs one, say so."
            )
        return "\n\n".join(parts)

    # -- size accounting ----------------------------------------------------------- #

    def set_static(self, instructions: str, tools_tokens: int, tool_count: int) -> None:
        self._instructions = estimate_tokens(instructions)
        self._tools = tools_tokens
        self._tool_count = tool_count

    def observe(self, info: Optional[Dict[str, Any]]) -> None:
        """The server's report of the context (`{"tokens", "window", "percent"}`)."""
        if not info:
            return
        self._tokens = int(info.get("tokens") or 0) or None
        if info.get("window"):
            self.window = int(info["window"])

    def breakdown(self) -> Breakdown:
        project = estimate_tokens(self.project_instructions())
        summary = estimate_tokens(self.summary)
        fixed = self._instructions + project + self._tools
        # What the server reported includes Clara's own prompt and memory: it all counts as messages
        used = self._tokens if self._tokens is not None else fixed + summary
        return Breakdown(
            window=self.window,
            used=used,
            instructions=self._instructions,
            project=project,
            summary=summary,
            tools=self._tools,
            messages=max(0, used - fixed - summary),
            tool_count=self._tool_count,
        )

    @property
    def percent(self) -> float:
        return self.breakdown().percent
