"""The saved command history: capped, consecutive duplicates collapsed, lines that look like they hold a secret not kept."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..core.permissions.redact import looks_secret

CAP = 2000


class History:
    def __init__(self, path: Path, cap: int = CAP):
        self.path = path
        self.cap = cap
        self.entries: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            lines = [line for line in self.path.read_text(encoding="utf-8", errors="replace").split("\n") if line.strip()]
        except OSError:
            return []
        return lines[-self.cap:]

    def add(self, line: str) -> bool:
        """Remember a line. Returns False when it was not kept (empty, a repeat of the last, or secret-looking)."""
        line = line.rstrip("\n")
        if not line.strip() or "\n" in line or (self.entries and self.entries[-1] == line) or looks_secret(line):
            return False
        self.entries.append(line)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if len(self.entries) > self.cap * 1.25:
                self.entries = self.entries[-self.cap:]
                self._rewrite()
        except OSError:
            pass  # history is a convenience: never in the way
        return True

    def _rewrite(self) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".history-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("\n".join(self.entries) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def clear(self) -> None:
        self.entries = []
        with contextlib.suppress(OSError):
            self.path.unlink()

    def ghost(self, typed: str) -> str:
        """The rest of the most recent history line that starts with `typed` (empty when none)."""
        if not typed:
            return ""
        for line in reversed(self.entries):
            if len(line) > len(typed) and line.startswith(typed):
                return line[len(typed):]
        return ""

    def matching(self, prefix: str, limit: int = 50) -> list[str]:
        seen: list[str] = []
        for line in reversed(self.entries):
            if line.startswith(prefix) and line not in seen:
                seen.append(line)
                if len(seen) >= limit:
                    break
        return seen


class MessageHistory(History):
    """What was sent to the agent. A message may have several lines; each is kept on one line (`\\n` for a line break)."""

    @staticmethod
    def encode(text: str) -> str:
        return text.replace("\\", "\\\\").replace("\n", "\\n")

    @staticmethod
    def decode(line: str) -> str:
        out, index = [], 0
        while index < len(line):
            if line[index] == "\\" and index + 1 < len(line):
                out.append("\n" if line[index + 1] == "n" else line[index + 1])
                index += 2
            else:
                out.append(line[index])
                index += 1
        return "".join(out)

    def add_message(self, text: str) -> bool:
        return self.add(self.encode(text.strip()))

    def messages(self) -> list[str]:
        return [self.decode(line) for line in self.entries]
