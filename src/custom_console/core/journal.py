"""The journal: a JSON Lines file per session. What it records: turns, local tool calls with their arguments, permission
decisions with the reason, approval answers and errors. Secrets are redacted *before* writing; the file is capped and
rotated, and only the newest session files are kept. It never holds a message's text (that is on Clara: /transcript).
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .permissions.redact import redact_value

MAX_BYTES = 2_000_000  # one file; past this it is rotated to `.1`
KEEP_FILES = 30
MAX_FIELD = 4_000


def _shorten(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD:
        return f"{value[:MAX_FIELD]}… [+{len(value) - MAX_FIELD} characters]"
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v) for v in value[:100]]
    return value


class Journal:
    def __init__(self, folder: Path, session_id: str, clock: Callable[[], float] = time.time):
        self.path = folder / f"{session_id}.jsonl"
        self._clock = clock

    def write(self, kind: str, **fields: Any) -> None:
        record = redact_value(_shorten({"t": round(self._clock(), 3), "kind": kind, **fields}))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # a journal that cannot be written must never stop the work

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
        except FileNotFoundError:
            return
        old = self.path.with_suffix(".jsonl.1")
        with contextlib.suppress(OSError):
            old.unlink()
        self.path.replace(old)

    # ------------------------------------------------------------------ convenience
    def turn(self, turn_id: str, conversation: str | None, model: str, outcome: str, seconds: float, chars: int) -> None:
        self.write("turn", id=turn_id, conversation=conversation, model=model, outcome=outcome, seconds=round(seconds, 2),
                   message_chars=chars)

    def tool(self, name: str, arguments: dict[str, Any], decision: str, step: str, reason: str, answer: str, ok: bool,
             seconds: float, result_chars: int) -> None:
        self.write("tool", name=name, arguments=arguments, decision=decision, step=step, reason=reason, answer=answer, ok=ok,
                   seconds=round(seconds, 2), result_chars=result_chars)

    def approval(self, approval_id: int, tool: str, answer: str, by: str) -> None:
        self.write("approval", id=approval_id, tool=tool, answer=answer, by=by)

    def error(self, code: str, message: str) -> None:
        self.write("error", code=code, message=message)

    def read(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
        return out


def prune_journals(folder: Path, keep: int = KEEP_FILES) -> int:
    """Keep the newest `keep` session files (rotated copies go with their file)."""
    try:
        files = sorted((p for p in folder.glob("*.jsonl")), key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    old = files[: max(len(files) - keep, 0)]
    for path in old:
        for target in (path, path.with_suffix(".jsonl.1")):
            with contextlib.suppress(OSError):
                target.unlink()
    return len(old)
