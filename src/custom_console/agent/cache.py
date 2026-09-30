"""Small JSON key/value cache with an optional time-to-live."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class JsonCache:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, max_age: Optional[float] = None) -> Any:
        """Cached value, or None when absent or older than `max_age` seconds."""
        entry = self._load().get(key)
        if not isinstance(entry, dict) or "data" not in entry:
            return None
        if max_age is not None and time.time() - entry.get("fetched_at", 0) > max_age:
            return None
        return entry["data"]

    def set(self, key: str, value: Any) -> None:
        data = self._load()
        data[key] = {"fetched_at": time.time(), "data": value}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.path)  # atomic: never leaves a half-written cache
        except OSError:
            pass  # a cache that cannot be written is only a missed optimisation

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
