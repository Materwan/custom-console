"""JSON Lines journal of the agent's exchanges (prompts, answers, tool calls)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class JsonlLogger:
    """Appends one JSON object per line.

    Every entry has a UTC ISO-8601 ``timestamp`` and a ``type`` ("prompt",
    "answer", "tool_call" or "error"). The file is rotated to ``<name>.1``
    once it exceeds `max_bytes`. Logging never raises: a failing journal must
    not break the agent.
    """

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size >= self.max_bytes:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass

    def _write(self, entry: Dict[str, Any]) -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def log_prompt(self, prompt: str) -> None:
        self._write({"type": "prompt", "prompt": prompt})

    def log_answer(self, answer: str) -> None:
        self._write({"type": "answer", "answer": answer})

    def log_error(self, message: str) -> None:
        self._write({"type": "error", "error": message})

    def log_tool_call(
        self,
        name: str,
        arguments: Dict[str, Any],
        result: Any,
        duration: float,
        error: Optional[str] = None,
    ) -> None:
        self._write(
            {
                "type": "tool_call",
                "tool": name,
                "arguments": arguments,
                "duration_seconds": round(duration, 4),
                "result": result,
                "error": error,
            }
        )
