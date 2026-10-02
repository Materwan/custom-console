"""The free zone: the folder (and subfolders) where file tools need no permission.

Everywhere else the tools keep their normal permission level. The zone is fixed
when the agent starts; it does not follow the agent's ``cd``. Paths are compared
after resolving symbolic links, so a link inside the zone that points outside
does not widen it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _key(path: "str | os.PathLike[str]") -> str:
    return os.path.normcase(os.path.realpath(path))


class FreeZone:
    def __init__(self, root: "str | os.PathLike[str] | None" = None, reason: str = ""):
        self.root: Optional[Path] = Path(os.path.realpath(root)) if root else None
        self.reason = reason  # why there is no zone, when `root` is None
        self._key = _key(self.root) if self.root else ""

    @classmethod
    def around(cls, directory: "str | os.PathLike[str] | None") -> "FreeZone":
        """Zone for `directory`, or an empty zone (with the reason) when it would
        be too wide or is not a real local folder."""
        if not directory:
            return cls(reason="the agent started outside a local folder")
        path = Path(directory)
        if not path.is_dir():
            return cls(reason=f"{directory} is not a local folder")
        resolved = Path(os.path.realpath(path))
        home = Path(os.path.realpath(Path.home()))
        if resolved.parent == resolved:
            return cls(reason="a drive root is too wide to be trusted")
        if resolved == home or resolved in home.parents:
            return cls(reason="your home folder (or one of its parents) is too wide to be trusted")
        return cls(resolved)

    @property
    def active(self) -> bool:
        return self.root is not None

    def contains(self, path: "str | os.PathLike[str]", *, strict: bool = False) -> bool:
        """Is `path` inside the zone? With `strict`, the zone folder itself does
        not count (removing or moving it is never free)."""
        if self.root is None:
            return False
        target = _key(path)
        if target == self._key:
            return not strict
        return target.startswith(self._key.rstrip(os.sep) + os.sep)

    def describe(self) -> str:
        return str(self.root) if self.root else f"none ({self.reason})"
