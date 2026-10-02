"""A question the agent asks the user (`ask_user`), and the user's answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Choice:
    label: str
    description: str = ""


@dataclass
class Answer:
    selected: List[str] = field(default_factory=list)  # labels of the options picked
    other: str = ""  # what the user typed instead of (or besides) an option

    def describe(self) -> str:
        """``SQLite, “keep both”``: the answer on one line."""
        parts = list(self.selected)
        if self.other:
            parts.append(f"“{self.other}”")
        return ", ".join(parts)
