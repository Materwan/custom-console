"""Uniform result type returned by every agent tool."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Generic, Optional, TypeVar

T = TypeVar("T")


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

    def to_dict(self) -> Dict[str, Any]:
        """Compact form: unset `data`/`error` are omitted to save tokens."""
        result: Dict[str, Any] = {"success": self.success}
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = f"{type(self.error).__name__}: {self.error}"
        return result

    def to_llm(self) -> str:
        """What the model sees: the result as compact JSON text."""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str, separators=(",", ":"))
