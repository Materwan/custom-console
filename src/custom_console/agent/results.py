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

    @classmethod
    def ok(cls, data: Optional[T] = None) -> "ToolResult[T]":
        return cls(success=True, data=data)

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
