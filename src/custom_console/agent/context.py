"""Conversation context: size accounting, project instructions and compaction.

The model's context window holds the system prompt, the tool definitions and
the conversation. This module tracks how full it is, loads the project's
instructions file, and replaces a long conversation by a summary (*compaction*)
by starting a new agno session that begins with that summary.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..llm.ollama import ModelInfo

if TYPE_CHECKING:
    from .tools.state import StaleFile

CHARS_PER_TOKEN = 3.5  # rough average for mixed prose and code
DEFAULT_WINDOW = 32_768
LOCAL_WINDOW_CAP = 32_768  # context requested from local models unless AGENT_NUM_CTX says otherwise
PROJECT_FILE_LIMIT = 8_000  # characters of the project instructions file that are used
MAX_TRANSCRIPT_CHARS = 60_000
MESSAGE_CHARS = 1_500  # kept of each message when building the transcript to summarise
TOOL_RESULT_CHARS = 300

COMPACT_PROMPT = (
    "You are summarising a conversation between a user and an AI assistant so that the "
    "assistant can continue the work without the original messages.\n"
    "Write a factual summary (at most 500 words) that keeps: the user's goals and "
    "instructions and preferences; decisions taken; files that were read, created or "
    "modified (with their paths); commands that were run and what they showed; errors and "
    "how they were solved; what remains to be done.\n"
    "Write it in the language the user speaks. Do not add commentary or greetings."
)


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STALE_FILES_SHOWN = 10
NOTES_START = "[Automatic note, not written by the user."
NOTES_BLOCK = re.compile(r"^\s*" + re.escape(NOTES_START) + r".*?\]\n\n", re.DOTALL)


def describe_now(now: datetime) -> str:
    """``2026-10-01 14:32 (Thursday, UTC+02:00)``: no locale, no ambiguity."""
    offset = now.strftime("%z") or "+0000"
    return f"{now:%Y-%m-%d %H:%M} ({WEEKDAYS[now.weekday()]}, UTC{offset[:3]}:{offset[3:]})"


def _when(timestamp: float, now: datetime) -> str:
    moment = datetime.fromtimestamp(timestamp, tz=now.tzinfo)
    return f"at {moment:%H:%M}" if moment.date() == now.date() else f"on {moment:%Y-%m-%d} at {moment:%H:%M}"


def turn_notes(now: datetime, stale: Sequence["StaleFile"] = ()) -> str:
    """What the agent is told with each message: the date and time, and the files that
    changed since it read them (its earlier reads of them are outdated).

    It goes before the user's message rather than into the system prompt: the
    system prompt and the history then stay the same from one turn to the next, so
    the model's prompt cache (Ollama, OpenAI) is not invalidated every turn.

    Its form was chosen by trying it on a small local model (qwen3.5:4b): an
    XML-like ``<context>`` block before the message made it end its turn without
    an answer most of the time; a bracketed note saying it is not from the user did not.
    """
    lines = [f"{NOTES_START} Current date and time: {describe_now(now)}."]
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
    return "\n".join(lines) + "]"


def without_notes(text: str) -> str:
    """A user message without the block of :func:`turn_notes`."""
    return NOTES_BLOCK.sub("", text, count=1)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def choose_window(info: Optional[ModelInfo], requested: Optional[int]) -> Tuple[int, Optional[int]]:
    """``(window, num_ctx)``: the context size to show, and the size to request
    from Ollama (None for models served remotely, where it is not ours to set)."""
    if info is not None and info.remote:
        return info.context_length or DEFAULT_WINDOW, None
    maximum = info.context_length if info and info.context_length else None
    window = requested or min(maximum or LOCAL_WINDOW_CAP, LOCAL_WINDOW_CAP)
    if maximum:
        window = min(window, maximum)
    return window, window


def message_text(message: Any) -> str:
    """Everything a message contributes to the context, as text."""
    parts: List[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(str(content))
    for call in getattr(message, "tool_calls", None) or []:
        parts.append(json.dumps(call, default=str))
    return "\n".join(parts)


def estimate_messages(messages: Iterable[Any]) -> int:
    return sum(estimate_tokens(message_text(m)) for m in messages if getattr(m, "role", "") != "system")


def context_tokens_of(messages: Iterable[Any]) -> Optional[int]:
    """Context size reported by the model at its last call of a run: what it was
    given (prompt) plus what it answered."""
    for message in reversed(list(messages)):
        if getattr(message, "role", "") != "assistant":
            continue
        metrics = getattr(message, "metrics", None)
        total = (getattr(metrics, "input_tokens", 0) or 0) + (getattr(metrics, "output_tokens", 0) or 0)
        if total:
            return int(total)
    return None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def build_transcript(messages: Iterable[Any], budget: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The conversation as plain text for the summariser: long messages and tool
    results are cut, and the oldest messages are dropped when it is still too long."""
    lines: List[str] = []
    for message in messages:
        role = getattr(message, "role", "")
        if role == "system":
            continue
        text = message_text(message)
        if role == "tool":
            name = getattr(message, "tool_name", None) or "tool"
            lines.append(f"[{name} result] {_clip(text, TOOL_RESULT_CHARS)}")
        elif role == "assistant":
            calls = [
                (call.get("function") or {}).get("name", "?")
                for call in (getattr(message, "tool_calls", None) or [])
                if isinstance(call, dict)
            ]
            said = _clip(getattr(message, "content", "") or "", MESSAGE_CHARS)
            if calls:
                said = (said + " " if said else "") + f"[called: {', '.join(calls)}]"
            if said:
                lines.append(f"Assistant: {said}")
        elif role == "user":
            lines.append(f"User: {_clip(without_notes(text), MESSAGE_CHARS)}")

    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > budget:
            kept.append("[earlier messages omitted]")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(reversed(kept))


def summarize_with(
    chat: Callable[[List[Dict[str, str]]], str],
    transcript: str,
    previous_summary: str = "",
    focus: str = "",
) -> str:
    """Ask the model itself for the summary; `chat(messages)` returns its answer
    (see `Provider.chat`)."""
    request = ""
    if previous_summary:
        request += f"Summary of the conversation before these messages:\n{previous_summary}\n\n"
    request += f"Conversation:\n{transcript}"
    if focus:
        request += f"\n\nPay particular attention to: {focus}"

    summary = (
        chat(
            [
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": request},
            ]
        )
        or ""
    ).strip()
    if not summary:
        raise RuntimeError("the model returned an empty summary")
    return summary


Summarizer = Callable[[str, str, str], str]  # (transcript, previous summary, focus) -> summary


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
        compact_percent: int = 80,
    ) -> None:
        self.base_session_id = base_session_id
        self.window = window
        self.project_file = project_file
        self.compact_percent = compact_percent

        self.generation = 0
        self.summary = ""
        self.disabled_tools: List[str] = []  # tools the user turned off (/tools)
        self._instructions = 0
        self._tools = 0
        self._tool_count = 0
        self._messages = 0
        self._measured: Optional[int] = None

    # -- identity of the conversation ----------------------------------------- #

    @property
    def session_id(self) -> str:
        """agno session holding the conversation; compaction moves to a new one."""
        return self.base_session_id if not self.generation else f"{self.base_session_id}-{self.generation}"

    def state(self) -> Dict[str, Any]:
        """What identifies the conversation, for the saved session."""
        return {"base": self.base_session_id, "generation": self.generation, "summary": self.summary}

    def restore(self, state: Dict[str, Any]) -> None:
        """Go back to a conversation saved with `state()`."""
        self.base_session_id = str(state.get("base") or self.base_session_id)
        self.generation = int(state.get("generation", 0) or 0)
        self.summary = str(state.get("summary", ""))
        self._messages = 0
        self._measured = None

    # -- what goes into the prompt ---------------------------------------------- #

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
        parts: List[str] = []
        project = self.project_instructions()
        if project:
            parts.append(f"## Project instructions ({self.project_file.name})\n{project}")  # type: ignore[union-attr]
        if self.summary:
            parts.append(f"## Earlier in this conversation (summary)\n{self.summary}")
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

    def observe(self, measured: Optional[int], messages_estimate: int) -> None:
        """After a turn: what the model reported, and our estimate of the messages."""
        self._measured = measured
        self._messages = messages_estimate

    def breakdown(self) -> Breakdown:
        project = estimate_tokens(self.project_instructions())
        summary = estimate_tokens(self.summary)
        estimated = self._instructions + project + summary + self._tools + self._messages
        used = estimated
        if self._measured and self._measured >= estimated * 0.5:
            used = self._measured
        return Breakdown(
            window=self.window,
            used=used,
            instructions=self._instructions,
            project=project,
            summary=summary,
            tools=self._tools,
            messages=self._messages,
            tool_count=self._tool_count,
        )

    @property
    def percent(self) -> float:
        return self.breakdown().percent

    def should_compact(self) -> bool:
        return self.compact_percent > 0 and self.percent >= self.compact_percent

    # -- compaction / reset ----------------------------------------------------------- #

    def adopt_summary(self, summary: str) -> None:
        """Start a new session that begins with `summary`."""
        self.generation += 1
        self.summary = summary.strip()
        self._messages = 0
        self._measured = None

    def reset(self, base_session_id: Optional[str] = None) -> None:
        """Forget the conversation (``/clear``): a new session, no summary. With
        `base_session_id` the new session has that identity (it is kept apart
        from the old one); without, it follows the old one."""
        if base_session_id is not None:
            self.base_session_id = base_session_id
            self.generation = 0
        else:
            self.generation += 1
        self.summary = ""
        self._messages = 0
        self._measured = None
