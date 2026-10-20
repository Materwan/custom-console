"""Everything specific to an operating system, behind the interfaces of `base`. Standard library only."""

from __future__ import annotations

import sys

from .base import AppMatch, Cancelled, CancelToken, Desktop, DesktopError, FilePrivacy, PathRules, Platform

__all__ = [
    "AppMatch", "CancelToken", "Cancelled", "Desktop", "DesktopError", "FilePrivacy", "PathRules", "Platform", "current",
    "for_system",
]

_current: Platform | None = None


def for_system(system: str) -> Platform:
    """The implementation for `windows`, `linux` or `macos`."""
    if system == "windows":
        from . import windows
        return windows.make()
    if system == "macos":
        from . import macos
        return macos.make()
    from . import linux
    return linux.make()


def current() -> Platform:
    """The platform this program runs on (built once)."""
    global _current
    if _current is None:
        system = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
        _current = for_system(system)
    return _current
