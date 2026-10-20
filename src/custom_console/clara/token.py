"""Reading the token file. The token is the console's only secret: it never goes to the environment or a log."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .errors import ClaraError
from .types import TOKEN_SHAPE

Privacy = Callable[[Path], str | None]  # a problem with the file's permissions, or None when only you can read it


class TokenFileError(ClaraError):
    def __init__(self, message: str):
        super().__init__("token_file", message)


def read_token(path: Path, privacy: Privacy | None = None) -> str:
    """The token in `path`. A file others can read is refused (`privacy` says so), and so is one that is missing,
    empty, or does not look like `clara_<id>_<secret>`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TokenFileError(f"No token file at {path}: run `custom_console --save-token` first.") from None
    except (OSError, UnicodeDecodeError) as error:
        raise TokenFileError(f"Cannot read the token file {path}: {error}") from None
    problem = privacy(path) if privacy else None
    if problem:
        raise TokenFileError(problem)
    token = text.strip()
    if not token:
        raise TokenFileError(f"The token file {path} is empty.")
    if not TOKEN_SHAPE.match(token):
        raise TokenFileError(f"{path} does not hold a Clara token (clara_<id>_<secret>).")
    return token
