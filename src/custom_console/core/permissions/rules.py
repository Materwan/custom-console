"""The console's own rules: "always" and "never" answers, kept in `rules.json` and shown by `/rules`.

A rule stores the tool, the exact command and *all* of its paths together, and matches only that same call (SEC-3). It is
never applied to a call the floor or a tainted turn escalated (N1). A refusal rule is the only kind that always applies:
saying no is always safe.

A *pattern* rule (`local_run`, `uv run pytest:*`) is the other kind of "always": the words that say what runs, then any
arguments, for one folder only (the hash of its real path, as in `trust.json`). It is checked again each time it is used
(`pattern_covers`), so a hand-edited `uv:*` never matches anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..vfs.text import fold_path
from .allowlist import Simple, pattern_covers


def folder_id(real_folder: str, case_insensitive: bool) -> str:
    """The folder a pattern rule belongs to: a hash of its real path, so the file shows nothing about where you work."""
    return hashlib.sha256(fold_path(real_folder, case_insensitive).rstrip("/").encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RuleKey:
    """What a rule is about: this tool, this exact command, these real paths and no others."""

    tool: str
    command: str = ""
    paths: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {"tool": self.tool, "command": self.command, "paths": list(self.paths)}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> RuleKey:
        paths = data.get("paths")
        return cls(str(data.get("tool", "")), str(data.get("command", "")),
                   tuple(sorted(str(p) for p in paths)) if isinstance(paths, list) else ())

    def describe(self) -> str:
        parts = [self.tool]
        if self.command:
            parts.append(repr(self.command))
        if self.paths:
            parts.append("on " + ", ".join(self.paths))
        return " ".join(parts)


@dataclass(slots=True)
class Rule:
    effect: str  # "allow" or "refuse"
    key: RuleKey
    created: float = 0.0
    hits: int = 0
    note: str = ""
    id: int = field(default=0)
    pattern: tuple[str, ...] = ()  # non-empty: a pattern rule, the key holds only its tool
    folder: str = ""  # a pattern rule applies in this folder only

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {"effect": self.effect, **self.key.to_json(), "created": self.created, "hits": self.hits,
                                   "note": self.note}
        if self.pattern:
            data |= {"pattern": list(self.pattern), "folder": self.folder}
        return data

    def describe(self, here: str = "") -> str:
        """The rule in a line; a pattern rule says whether it is for the folder the person is in."""
        if not self.pattern:
            return self.key.describe()
        where = "in this folder" if self.folder == here else f"in another folder ({self.folder[:8]})"
        return f"{self.key.tool} '{' '.join(self.pattern)}:*' {where}"


class RuleStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock

    def _load(self) -> list[Rule]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("rules") if isinstance(data, dict) else None
        rules: list[Rule] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("effect") in ("allow", "refuse") and item.get("tool"):
                words = item.get("pattern")
                pattern = tuple(str(w) for w in words) if isinstance(words, list) else ()
                rules.append(Rule(str(item["effect"]), RuleKey.from_json(item), float(item.get("created", 0) or 0),
                                  int(item.get("hits", 0) or 0), str(item.get("note", "")), pattern=pattern,
                                  folder=str(item.get("folder", "")) if pattern else ""))
        for number, rule in enumerate(rules, 1):
            rule.id = number
        return rules

    def _save(self, rules: list[Rule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".rules-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"rules": [r.to_json() for r in rules]}, handle, indent=1)
        os.replace(temporary, self.path)

    def list(self) -> list[Rule]:
        return self._load()

    def add(self, effect: str, key: RuleKey, note: str = "") -> Rule:
        if effect not in ("allow", "refuse"):
            raise ValueError("effect must be allow or refuse")
        rules = self._load()
        for rule in rules:
            if not rule.pattern and rule.key == key and rule.effect == effect:
                return rule
        rules = [r for r in rules if r.pattern or r.key != key]  # a new answer replaces the opposite one
        rule = Rule(effect, key, self._clock(), 0, note, len(rules) + 1)
        rules.append(rule)
        self._save(rules)
        return rule

    def remove(self, rule_id: int) -> bool:
        rules = self._load()
        kept = [r for r in rules if r.id != rule_id]
        if len(kept) == len(rules):
            return False
        self._save(kept)
        return True

    def match(self, key: RuleKey, *, effect: str) -> Rule | None:
        """The rule of this effect for exactly this key, counting the hit."""
        rules = self._load()
        for rule in rules:
            if rule.effect == effect and not rule.pattern and rule.key == key:
                rule.hits += 1
                self._save(rules)
                return rule
        return None

    # ------------------------------------------------------------------ pattern rules
    def add_pattern(self, tool: str, pattern: tuple[str, ...], folder: str, note: str = "") -> Rule:
        rules = self._load()
        for rule in rules:
            if rule.pattern == pattern and rule.folder == folder and rule.key.tool == tool and rule.effect == "allow":
                return rule
        rule = Rule("allow", RuleKey(tool, " ".join(pattern) + ":*"), self._clock(), 0, note, len(rules) + 1,
                    tuple(pattern), folder)
        rules.append(rule)
        self._save(rules)
        return rule

    def find_pattern(self, tool: str, simple: Simple, folder: str, windows: bool) -> Rule | None:
        """The stored pattern of this folder that covers the command (it is checked again now), without counting a hit."""
        for rule in self._load():
            if (rule.pattern and rule.effect == "allow" and rule.key.tool == tool and rule.folder == folder
                    and pattern_covers(rule.pattern, simple, windows)):
                return rule
        return None

    def hit(self, rules_used: Iterable[Rule]) -> None:
        used = {r.id for r in rules_used}
        rules = self._load()
        for rule in rules:
            if rule.id in used:
                rule.hits += 1
        self._save(rules)
