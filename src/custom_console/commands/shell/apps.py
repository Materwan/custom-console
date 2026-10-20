"""The apps `launch` has found before, remembered in a file so the next start is instant."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


class AppCache:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self) -> None:
        temporary = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".apps-")
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._items, handle, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            if temporary:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)  # the cache is a convenience: a failed save loses nothing important

    @staticmethod
    def key(name: str) -> str:
        key = name.strip().lower()
        return key[:-4] if key.endswith(".exe") else key

    def get(self, name: str) -> str | None:
        """The remembered target, if it still exists (a stale entry is dropped)."""
        target = self._items.get(self.key(name))
        if target is None:
            return None
        if not Path(target).exists():
            del self._items[self.key(name)]
            self._save()
            return None
        return target

    def remember(self, name: str, target: str) -> None:
        self._items[self.key(name)] = target
        self._save()

    def names(self) -> list[str]:
        return sorted(self._items)
