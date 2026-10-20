"""The interfaces of everything that is specific to an operating system.

One interface per job (file privacy, desktop, path rules), each with a Windows, a Linux and a macOS implementation.
Nothing above this layer asks which system it runs on: it asks the `Platform` it was given, so a test can pass a fake.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Cancelled(Exception):
    """An operation was stopped through its CancelToken."""


class CancelToken:
    """One flag, passed into every backend call and every process, so Esc can stop work that is running in a thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout` seconds; True when cancelled meanwhile."""
        return self._event.wait(timeout)


class DesktopError(Exception):
    """The desktop cannot do this (no clipboard tool, no application found...). The message says why."""


@dataclass(frozen=True, slots=True)
class AppMatch:
    """An application the finder found: what to show, and what to start."""

    name: str
    target: str  # an executable path, a .desktop file or an .app bundle
    stage: int = 1  # the search stage that found it (1 = PATH ... 4 = recursive walk)


class FilePrivacy(Protocol):
    """Files only you may read: the token, the data folder."""

    tightens: bool
    """True when a folder that is too open may be fixed without asking (Linux, macOS); Windows only warns."""

    def problem(self, path: Path) -> str | None:
        """Why `path` can be read by others (a sentence with the fix), or None when only you can read it."""

    def make_private(self, path: Path) -> None:
        """Restrict a file or folder to you. May raise OSError."""

    def write_private(self, path: Path, text: str) -> None:
        """Write a file that was never readable by others, not even for a moment."""


class Desktop(Protocol):
    def open_path(self, path: Path) -> None:
        """Open a file or folder with its default application."""

    def find_app(self, name: str, level: int = 4) -> AppMatch | None:
        """Look for an installed application by name, running the first `level` search stages (1-4)."""

    def start_app(self, match: AppMatch) -> None:
        """Start an application found by `find_app`, detached from the console."""

    def start_file(self, path: Path) -> None:
        """Start an executable given as a path, detached."""

    def clipboard_read(self) -> str: ...

    def clipboard_write(self, text: str) -> None: ...

    def clipboard_image(self) -> bytes | None:
        """The clipboard's picture as PNG bytes, None when it holds no picture. DesktopError when unreadable."""

    def restart_console(self, argv: list[str]) -> None:
        """Start a fresh copy of the console with `argv` and leave this one (`reload`). May not return."""


class PathRules(Protocol):
    case_insensitive: bool
    """File names differ only by case on this system (Windows, macOS by default)."""

    def home(self) -> Path: ...

    def is_network_path(self, raw: str) -> bool:
        """A UNC path to another host (`\\\\host\\share`), which could send credentials there. WSL is not one."""

    def is_wsl_path(self, raw: str) -> bool: ...

    def credential_dirs(self, home: Path) -> tuple[Path, ...]:
        """Folders that hold secrets on this system: browser profiles, key stores, cloud credentials."""

    def drives(self) -> list[str]:
        """Drive letters that exist (`["C", "D"]`); empty where the file system has a single root."""

    def wsl_distros(self) -> list[str]:
        """Installed WSL distributions; empty where there is no WSL."""


@dataclass(frozen=True, slots=True)
class Platform:
    name: str  # "windows", "linux" or "macos"
    privacy: FilePrivacy
    desktop: Desktop
    paths: PathRules

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"
