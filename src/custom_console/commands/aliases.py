"""Aliases: `alias ll='ls -l'`, kept in a file in the data folder.

Only the first word of a command is expanded, and only once, so an alias cannot loop.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_.\-+]+$")
PROTECTED = frozenset({"alias", "unalias"})  # so a mistake can always be undone


class Aliases:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and NAME.match(k)} if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".aliases-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._items, handle, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def get(self, name: str) -> str | None:
        return self._items.get(name)

    def all(self) -> dict[str, str]:
        return dict(sorted(self._items.items()))

    def set(self, name: str, value: str) -> None:
        if not NAME.match(name):
            raise ValueError(f"{name!r} is not a valid alias name (letters, digits, and _ . - +)")
        if name in PROTECTED:
            raise ValueError(f"{name} cannot be an alias")
        if not value.strip():
            raise ValueError("an alias needs a value")
        self._items[name] = value
        self._save()

    def remove(self, name: str) -> bool:
        if name not in self._items:
            return False
        del self._items[name]
        self._save()
        return True

    def clear(self) -> int:
        count = len(self._items)
        self._items = {}
        self._save()
        return count

    def names(self) -> list[str]:
        return sorted(self._items)
