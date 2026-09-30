"""Parsing of the *virtual* paths exposed by the file manager.

The console presents a single virtual tree rooted at ``/``::

    /                    virtual root (menu)
    /C:/Users/...        a Windows drive
    /wsl-Ubuntu/home/... a WSL distribution (``\\\\wsl$\\Ubuntu``)
    /reMarkable/Notes    the reMarkable tablet (through rmapi)

``reMarkable:<path>`` is also accepted as an explicit (possibly relative)
reference to the tablet. This module is the single place where these
conventions are understood.
"""

from __future__ import annotations

import posixpath
import re
import string
import os
from dataclasses import dataclass
from typing import List, Literal, Optional

REMARKABLE_ENTRY = "reMarkable"
_DRIVE_RE = re.compile(r"[a-zA-Z]:")
_REMOTE_PREFIX = "remarkable:"


def wsl_entry(distro: str) -> str:
    """Name of the WSL entry in the virtual root (e.g. ``wsl-Ubuntu``)."""
    return f"wsl-{distro}"


@dataclass(frozen=True)
class VirtualPath:
    kind: Literal["remarkable", "wsl", "drive"]
    rest: str = ""  # path below the mount point, "/" separated, no leading slash
    drive: str = ""  # "C:" when kind == "drive"


def parse(raw: str, wsl_distro: str, *, absolute: bool = True) -> Optional[VirtualPath]:
    """Recognise a virtual path.

    With ``absolute=True`` the path must start with a slash
    (``/reMarkable/x``, ``/C:/x``). With ``absolute=False`` the leading slash is
    optional, which is what relative paths typed from the virtual root use
    (``reMarkable/x``, ``C:/x``). Returns ``None`` for anything else.
    """
    text = raw.replace("\\", "/")
    if absolute:
        if not text.startswith("/"):
            return None
        text = text.lstrip("/")

    first, _, rest = text.partition("/")
    rest = rest.strip("/")
    lowered = first.lower()

    if lowered == REMARKABLE_ENTRY.lower():
        return VirtualPath("remarkable", rest)
    if lowered == wsl_entry(wsl_distro).lower():
        return VirtualPath("wsl", rest)
    if _DRIVE_RE.fullmatch(first):
        return VirtualPath("drive", rest, drive=first.upper())
    return None


def local_path(path: VirtualPath, wsl_distro: str) -> str:
    """Real local path (forward slashes) of a ``drive`` / ``wsl`` virtual path."""
    if path.kind == "drive":
        base = f"{path.drive}/"
    elif path.kind == "wsl":
        base = f"//wsl$/{wsl_distro}/"
    else:
        raise ValueError(f"{path.kind!r} is not a local mount")
    return base + path.rest if path.rest else base


def remote_path(path: VirtualPath) -> str:
    """Absolute reMarkable path (posix) of a ``remarkable`` virtual path."""
    if path.kind != "remarkable":
        raise ValueError(f"{path.kind!r} is not the reMarkable mount")
    return posixpath.normpath("/" + path.rest) if path.rest else "/"


def strip_remote_prefix(raw: str) -> Optional[str]:
    """``reMarkable:Notes`` -> ``Notes`` (``.`` when empty); ``None`` otherwise."""
    if raw.lower().startswith(_REMOTE_PREFIX):
        remainder = raw[len(_REMOTE_PREFIX) :]
        return remainder or "."
    return None


def local_to_virtual(path: str) -> str:
    """Express an absolute local path as a virtual path that resolves to the
    local file system whatever the current mode (``C:\\x`` -> ``/C:/x``)."""
    normalized = path.replace("\\", "/")
    if _DRIVE_RE.match(normalized[:2]):
        return "/" + normalized
    return normalized


def list_drives() -> List[str]:
    """Windows drive letters currently mounted (``["C:/", "D:/"]``)."""
    return [f"{letter}:/" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:/")]


def root_entries(wsl_distro: str) -> List[str]:
    """Entries listed in the virtual root (directories, with trailing slash)."""
    return list_drives() + [f"{REMARKABLE_ENTRY}/", f"{wsl_entry(wsl_distro)}/"]
