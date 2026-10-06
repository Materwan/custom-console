"""Uniform result type returned by every agent tool, and how the model reads it.

The model gets plain text, never JSON with escaped strings: a file read with ``\\n`` and ``\\"``
escapes would have to be unescaped again when the model copies a piece of it into an edit, which
small models get wrong. A failure starts with ``Error:``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Generic, Optional, TypeVar

T = TypeVar("T")

ERROR_PREFIX = "Error: "
EMPTY = "(empty)"
DONE = "Done."


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def render_data(data: Any) -> str:
    """Plain text of a tool's data: text as it is; a list of texts one per line; a mapping as
    ``key: value`` lines, its multi-line texts as blocks at the end; anything else as compact JSON."""
    if data is None:
        return DONE
    if isinstance(data, str):
        return data if data else EMPTY
    if isinstance(data, (list, tuple)):
        if not data:
            return EMPTY
        if all(isinstance(item, str) for item in data):
            return "\n".join(data)
        return "\n".join(_scalar(item) for item in data)
    if isinstance(data, dict):
        lines, blocks = [], []
        for key, value in data.items():
            if isinstance(value, str) and "\n" in value:
                blocks.append(f"{key}:\n{value}")
            else:
                lines.append(f"{key}: {_scalar(value)}")
        return "\n".join(lines + blocks) or EMPTY
    return _scalar(data)


@dataclass
class ToolResult(Generic[T]):
    """Outcome of a tool call.

    - ``success`` -> `data` holds the result and `error` is None.
    - failure     -> `error` holds the exception; `data` may hold partial results.
    """

    success: bool
    data: Optional[T] = None
    error: Optional[BaseException] = None
    # Shown to the user in the turn, never sent to the model.
    diff: Optional[str] = None  # unified diff of a file change
    todos: Optional[str] = None  # the current checklist
    summary: Optional[str] = None  # a few words for the tool's line (default: from the data)
    detail: Optional[str] = None  # what the tool's line hides (default: the diff, or the data)

    @classmethod
    def ok(cls, data: Optional[T] = None, **display: Optional[str]) -> "ToolResult[T]":
        return cls(success=True, data=data, **display)

    @classmethod
    def fail(cls, error: BaseException, data: Optional[T] = None) -> "ToolResult[T]":
        return cls(success=False, data=data, error=error)

    @property
    def error_text(self) -> str:
        return f"{type(self.error).__name__}: {self.error}" if self.error is not None else ""

    def to_dict(self) -> Dict[str, Any]:
        """Structured form, for the journal: unset `data`/`error` are omitted."""
        result: Dict[str, Any] = {"success": self.success}
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = self.error_text
        return result

    def to_llm(self) -> str:
        """What the model sees: plain text; a failure starts with ``Error:``."""
        if self.success:
            return render_data(self.data)
        text = ERROR_PREFIX + self.error_text
        if self.data is not None:
            text += "\n" + render_data(self.data)
        return text


def is_error(text: str) -> bool:
    """Is this what the model was told of a failed call?"""
    return text.startswith(ERROR_PREFIX)
