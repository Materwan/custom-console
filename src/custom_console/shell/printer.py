"""Colored output helpers shared by every command.

All user-provided text (paths, file contents, error messages) is printed with
``markup=False``: rich markup such as ``[red]`` inside a file name or a file's
content must be shown verbatim, never interpreted.
"""

from __future__ import annotations

from typing import Iterable, Optional

from rich.console import Console
from rich.status import Status
from rich.text import Text

from ..fs import Permissions
from .tokenizer import quote_if_needed

# Central palette, so that every command looks consistent.
COLOR_PATH = "yellow"  # current location (prompt, pwd, stat...)
COLOR_PERM_OK = "green"  # granted permission (r/w/x)
COLOR_PERM_KO = "red"  # denied permission
COLOR_DIR = "bold blue"  # directories in listings
COLOR_INFO = "cyan"  # neutral info (resolved paths, find results...)
COLOR_WARNING = "yellow"
COLOR_SUCCESS = "green"
COLOR_ERROR = "bold red"
COLOR_DIM = "dim italic"


def permission_flags(perms: Permissions) -> Text:
    """``rwx`` with granted letters in green and denied ones shown as a red ``-``."""
    text = Text()
    for granted, letter in zip(perms, "rwx"):
        text.append(letter if granted else "-", style=COLOR_PERM_OK if granted else COLOR_PERM_KO)
    return text


class Printer:
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    # -- plain messages ----------------------------------------------------- #

    def plain(self, message: object = "", style: Optional[str] = None) -> None:
        self.console.print(str(message), style=style, markup=False, highlight=False)

    def text(self, content: str) -> None:
        """Print verbatim text (file contents) without re-wrapping it."""
        end = "" if content.endswith("\n") else "\n"
        self.console.print(content, end=end, markup=False, highlight=False, soft_wrap=True)

    def error(self, message: object) -> None:
        self.plain(message, COLOR_ERROR)

    def warning(self, message: object) -> None:
        self.plain(message, COLOR_WARNING)

    def success(self, message: object) -> None:
        self.plain(message, COLOR_SUCCESS)

    def info(self, message: object) -> None:
        self.plain(message, COLOR_INFO)

    def blank(self) -> None:
        self.console.line()

    # -- structured output -------------------------------------------------- #

    def location(self, location: str, perms: Optional[Permissions] = None) -> None:
        line = Text(location, style=COLOR_PATH)
        if perms is not None:
            line.append(" ")
            line.append_text(permission_flags(perms))
        self.console.print(line)

    def listing(self, names: Iterable[str]) -> None:
        """Names on one wrapped line; directories (trailing ``/``) in blue."""
        line = Text()
        for index, name in enumerate(names):
            if index:
                line.append("  ")
            line.append(quote_if_needed(name), style=COLOR_DIR if name.endswith("/") else None)
        self.console.print(line)

    def tree_line(self, line: str) -> None:
        if line.endswith("/"):
            self.plain(line, COLOR_DIR)
        elif line.strip() == "...":
            self.plain(line, COLOR_DIM)
        else:
            self.plain(line)

    def status(self, message: str) -> Status:
        return self.console.status(message)


class _NoStatus:
    """Stand-in for a rich status when no live display is possible."""

    def __enter__(self) -> "_NoStatus":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def update(self, *args, **kwargs) -> None:
        return None


class QuietPrinter(Printer):
    """A printer whose console renders into a string: no spinners, which would
    write control codes into the captured text."""

    def status(self, message: str):  # type: ignore[override]
        return _NoStatus()
