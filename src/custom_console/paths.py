"""Where the console keeps its files, per platform: the config folder, the data folder, the Clara token file.

Windows: `%APPDATA%` for the config and the token, `%LOCALAPPDATA%` for the data.
Linux: the XDG folders. macOS: `~/Library/Application Support`. The token file is shared with Clara's own command-line
client, so it sits in Clara's folder (`clara/token`), not in the console's.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .platform import FilePrivacy

APP = "custom-console"


@dataclass(frozen=True, slots=True)
class Locations:
    config: Path  # `.env`
    data: Path  # everything the console writes
    token_file: Path  # shared with Clara's own client


def system_name() -> str:
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"


def locations(system: str, env: Mapping[str, str], home: Path) -> Locations:
    """The folders for `system` (`windows`, `linux` or `macos`), from an environment: pure, so it is tested everywhere."""
    if system == "windows":
        roaming = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return Locations(roaming / APP, local / APP / "data", roaming / "clara" / "token")
    if system == "macos":
        support = home / "Library" / "Application Support"
        return Locations(support / APP, support / APP / "data", home / ".config" / "clara" / "token")
    config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return Locations(config_home / APP, data_home / APP, config_home / "clara" / "token")


@dataclass(frozen=True, slots=True)
class DataLayout:
    """The files inside the data folder."""

    root: Path

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def undo(self) -> Path:
        return self.root / "undo"

    @property
    def journal(self) -> Path:
        return self.root / "journal"

    @property
    def rules(self) -> Path:
        return self.root / "rules.json"

    @property
    def trust(self) -> Path:
        return self.root / "trust.json"

    @property
    def usage(self) -> Path:
        return self.root / "usage.jsonl"

    @property
    def history(self) -> Path:
        return self.root / "history"

    @property
    def agent_history(self) -> Path:
        return self.root / "agent_history"

    @property
    def aliases(self) -> Path:
        return self.root / "aliases.json"

    @property
    def apps(self) -> Path:
        return self.root / "apps.json"

    @property
    def log(self) -> Path:
        return self.root / "console.log"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def folders(self) -> tuple[Path, ...]:
        return (self.root, self.sessions, self.undo, self.journal, self.tmp)


def prepare_folders(layout: DataLayout, privacy: FilePrivacy, extra: Sequence[Path] = ()) -> list[str]:
    """Create the data folders readable by you only, and check them. Returns what to tell the user: on Linux and macOS a
    folder that is too open is tightened (and said so); on Windows the access list is checked and a problem is a warning."""
    notes: list[str] = []
    for folder in (*layout.folders(), *extra):
        existed = folder.exists()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed and not privacy.tightens:
            continue  # a new folder under the user's profile inherits an access list for you alone
        problem = privacy.problem(folder)
        if problem is None:
            continue
        if privacy.tightens:
            try:
                privacy.make_private(folder)
                notes.append(f"Tightened {folder}: it could be read by others.")
                continue
            except OSError as error:
                problem = f"{problem} (could not fix it: {error.strerror or error})"
        notes.append(f"Warning: {problem}")
    return notes
