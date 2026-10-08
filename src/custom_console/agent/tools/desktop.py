"""Desktop tools (Windows): open a file or a URL, start an application, read or fill the clipboard.

Opening a program or a script runs it, so `open_path` on one is always asked; opening a document
or a URL may be approved for good ("always") per kind of file. Reading the clipboard asks unless
everything is auto-accepted (it often holds a password), and so does replacing what it holds.
"""

import os
import subprocess
import sys
from urllib.parse import urlsplit
from typing import Callable, List, Optional

from ...apps.finder import SavedApps, clean_name, find_application
from ...apps.launcher import launch
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded

CLIPBOARD_TIMEOUT = 15
CLIPBOARD_MAX_CHARS = 30_000  # what goes through an environment variable to PowerShell
URL_SCHEMES = ("http://", "https://", "mailto:")
EXECUTABLE = frozenset(
    {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".scr", ".lnk", ".url", ".reg", ".hta", ".cpl"}
)
APP_SEARCH_LEVEL = 3  # PATH, registry, install folders: not the slow recursive search


def _powershell(script: str, env: Optional[dict] = None) -> str:
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=CLIPBOARD_TIMEOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **(env or {})},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", errors="replace").strip() or "PowerShell failed")
    return done.stdout.decode("utf-8", errors="replace")


def desktop_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    if sys.platform != "win32":
        return []
    files = ctx.files

    def target_of(target: str) -> str:
        """A URL as it is, a path resolved to a local file."""
        target = target.strip()
        if target.lower().startswith(URL_SCHEMES):
            return target
        local = files.local_path(target, "open")
        if not os.path.exists(local):
            raise FileNotFoundError(f"{target} does not exist.")
        return local

    def open_rule(target: str) -> Optional[str]:
        if target.strip().lower().startswith(URL_SCHEMES):
            host = urlsplit(target.strip()).hostname or ""
            # per site: "always" for one address must not open any address later (a query string can carry data out)
            return f"open_path:{host.lower()}" if host and target.strip().lower().startswith("http") else None
        extension = os.path.splitext(target.strip())[1].lower()
        if not extension or extension in EXECUTABLE:
            return None  # a program, a script, a folder or something unknown: asked every time
        return f"open_path:{extension} files"

    @guarded(ctx, PermissionLevel.WRITE, rule=open_rule, describe=lambda target: f"Agent wants to open {target}")
    def open_path(target: str) -> ToolResult:
        """Open a file with its usual application, a folder in the explorer or a URL, on the user's screen.

        Args:
            target: a path or an http(s)/mailto URL
        """
        resolved = target_of(target)
        os.startfile(resolved)  # type: ignore[attr-defined]
        return ToolResult.ok(f"Opened {resolved}.")

    @guarded(
        ctx,
        PermissionLevel.WRITE,
        rule=lambda name: f"launch_app:{clean_name(name)}",
        describe=lambda name: f"Agent wants to start the application {name!r}",
    )
    def launch_app(name: str) -> ToolResult:
        """Start an application by name ("firefox", "code"), like the shell `launch`.

        Args:
            name: the application's name
        """
        path = find_application(name, level=APP_SEARCH_LEVEL, saved=SavedApps(ctx.settings.saved_apps_path))
        if not path:
            raise FileNotFoundError(f"No application named {name!r} was found.")
        launch(path)
        return ToolResult.ok(f"Started {path}.")

    @guarded(ctx, PermissionLevel.WRITE, describe=lambda: "Agent wants to read your clipboard")
    def clipboard_read() -> ToolResult:
        """The text in the user's clipboard."""
        text = _powershell("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Clipboard -Raw")
        text = text.replace("\r\n", "\n").rstrip("\n")
        return ToolResult.ok(text or "(the clipboard holds no text)")

    @guarded(ctx, PermissionLevel.WRITE, describe=lambda text: f"Agent wants to put {len(text)} characters in your clipboard")
    def clipboard_write(text: str) -> ToolResult:
        """Put text in the user's clipboard (replaces its content), for them to paste.

        Args:
            text: the text
        """
        if len(text) > CLIPBOARD_MAX_CHARS:
            raise ValueError(f"At most {CLIPBOARD_MAX_CHARS} characters.")
        _powershell("Set-Clipboard -Value $env:CLARA_CLIPBOARD", {"CLARA_CLIPBOARD": text})
        return ToolResult.ok(f"{len(text)} characters copied.")

    return [open_path, launch_app, clipboard_read, clipboard_write]
