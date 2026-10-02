"""The tools the agent may use: all of them, minus the ones turned off with ``/tools``.

The choice is kept in a small JSON file so that it survives restarts. A tool that
is turned off stays off even when it is absent for a while (Moodle disabled in
the settings, for instance).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence, Set, Tuple

Tool = Callable[..., Any]


class ToolSet:
    def __init__(self, groups: Sequence[Tuple[str, Sequence[Tool]]], path: Optional[Path] = None):
        self.groups: List[Tuple[str, List[Tool]]] = [(label, list(tools)) for label, tools in groups if tools]
        self.path = path
        self._disabled: Set[str] = set()
        self._load()

    # -- what exists ------------------------------------------------------------------- #

    def tools(self) -> List[Tool]:
        return [tool for _, tools in self.groups for tool in tools]

    def names(self) -> List[str]:
        return [tool.__name__ for tool in self.tools()]

    def group_of(self, name: str) -> str:
        for label, tools in self.groups:
            if any(tool.__name__ == name for tool in tools):
                return label
        raise KeyError(name)

    @staticmethod
    def describe(tool: Tool) -> str:
        """First sentence of the tool's documentation."""
        doc = " ".join((tool.__doc__ or "").split())
        first = doc.split(". ")[0].rstrip(".")
        return first

    # -- what is on ---------------------------------------------------------------------- #

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

    def enabled(self) -> List[Tool]:
        return [tool for tool in self.tools() if tool.__name__ not in self._disabled]

    def disabled_names(self) -> List[str]:
        return [name for name in self.names() if name in self._disabled]

    def set_enabled(self, states: "dict[str, bool]") -> None:
        """Turn tools on or off (`states` maps tool name to its new state)."""
        known = set(self.names())
        for name, on in states.items():
            if name not in known:
                continue
            if on:
                self._disabled.discard(name)
            else:
                self._disabled.add(name)
        self._save()

    def replace_disabled(self, names: Iterable[str], *, persist: bool = False) -> None:
        """Turn off exactly `names` (what a restored session used). The saved default
        is only rewritten with `persist`."""
        self._disabled = {str(name) for name in names}
        if persist:
            self._save()

    def reset(self) -> None:
        self._disabled.clear()
        self._save()

    # -- which names does the user mean ---------------------------------------------------- #

    def resolve(self, words: Iterable[str]) -> Tuple[List[str], List[str]]:
        """Tool names for the words typed: a tool name, a group name (``files``) or a
        unique prefix of a tool name. Returns ``(names, unknown words)``."""
        names = self.names()
        found: List[str] = []
        unknown: List[str] = []
        for word in words:
            lowered = word.lower()
            matches: List[str]
            if lowered in (name.lower() for name in names):
                matches = [name for name in names if name.lower() == lowered]
            elif lowered in (label.lower() for label, _ in self.groups):
                matches = [tool.__name__ for label, tools in self.groups if label.lower() == lowered for tool in tools]
            else:
                matches = [name for name in names if name.lower().startswith(lowered)]
                if len(matches) != 1:
                    matches = []
            if not matches:
                unknown.append(word)
            found.extend(name for name in matches if name not in found)
        return found, unknown

    # -- persistence ------------------------------------------------------------------------- #

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._disabled = {str(name) for name in data.get("disabled", [])}
        except (OSError, ValueError, AttributeError, TypeError):
            self._disabled = set()  # missing or damaged: everything is on

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"disabled": sorted(self._disabled)}, indent=2), encoding="utf-8")
        except OSError:
            pass  # the choice then only lasts for this run
