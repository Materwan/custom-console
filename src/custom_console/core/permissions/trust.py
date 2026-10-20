"""Trusted folders. Starting the agent in a new folder asks once; the answer is kept as a hash of the folder's real path,
not the path itself (so the file reveals nothing about where you work).

In an untrusted folder the agent gets no write or run tools, and `AGENT.md` is not loaded: a file there could carry
instructions meant for the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..vfs.text import fold_path


class TrustStore:
    def __init__(self, path: Path, case_insensitive: bool = False):
        self.path = path
        self._case_insensitive = case_insensitive

    def digest(self, real_folder: str) -> str:
        return hashlib.sha256(fold_path(real_folder, self._case_insensitive).rstrip("/").encode("utf-8")).hexdigest()

    def _load(self) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        folders = data.get("folders") if isinstance(data, dict) else None
        return {item for item in folders if isinstance(item, str)} if isinstance(folders, list) else set()

    def is_trusted(self, folder: str | Path) -> bool:
        return self.digest(os.path.realpath(folder)) in self._load()

    def trust(self, folder: str | Path) -> None:
        folders = self._load() | {self.digest(os.path.realpath(folder))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".trust-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"folders": sorted(folders)}, handle)
        os.replace(temporary, self.path)
