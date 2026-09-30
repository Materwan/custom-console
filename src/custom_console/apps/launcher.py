"""Start a program detached from the console."""

from __future__ import annotations

import os
import subprocess
import sys

_WINERROR_NOT_EXECUTABLE = 193  # shortcut (.lnk), document, ...


def launch(path: str) -> None:
    """Start `path` without attaching it to this console.

    Raises FileNotFoundError when the file does not exist, OSError when it
    cannot be started.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as error:
        # Not a real executable (.lnk, .url, associated document): only the
        # Windows shell can resolve it. The console detachment is lost there.
        if getattr(error, "winerror", None) == _WINERROR_NOT_EXECUTABLE and hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
            return
        raise


def relaunch_console() -> None:
    """Start a fresh copy of this console in a new window."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(list(sys.orig_argv), creationflags=flags)
