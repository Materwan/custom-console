"""Errors of the Clara client, and the one line (with its next step) the console shows for each code."""

from __future__ import annotations

from dataclasses import dataclass


class ClaraError(Exception):
    """A refused request or a failed turn: the server's `code` and `status`, and its message."""

    def __init__(self, code: str, message: str, status: int = 0, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        return str(describe_error(self.code, self.message, self.status))


class ConnectionFailed(ClaraError):
    """Clara could not be reached, or the connection died."""

    def __init__(self, message: str):
        super().__init__("connection", message)


class StreamSilent(ClaraError):
    """Nothing, not even a keepalive, arrived for longer than the read timeout: the connection is dead."""

    def __init__(self, seconds: float):
        super().__init__("silent", f"Clara sent nothing for {seconds:g} seconds.")


@dataclass(frozen=True)
class Described:
    line: str  # what happened
    next_step: str  # what to do about it ("" when the message already says it)

    def __str__(self) -> str:
        return f"{self.line} {self.next_step}".strip()


# code -> (what happened, next step). An empty first part means "use the server's message".
_KNOWN: dict[str, tuple[str, str]] = {
    "unauthorized": ("The token is wrong or revoked.", "Run `custom_console --save-token` with a valid token."),
    "too_large": ("The prompt is too large for the model.", "Try /compact, or send less."),
    "unprocessable": ("", ""),
    "conflict": ("", ""),
    "rate_limited": ("Too many requests, or too many wrong tokens.", "Wait a moment and retry."),
    "model_failed": ("The model failed.", "Retry, or choose another model with /model."),
    "unavailable": ("Clara is not available.", ""),
    "stopping": ("Clara is stopping.", "Try again in a minute."),
    "no_model": ("Clara has no usable model.", "Check the providers on the server."),
    "timeout": ("The model, or a local tool, took too long.", "Retry; run long commands in the background."),
    "not_found": ("", ""),
    "forbidden": ("", ""),
    "bad_request": ("", ""),
    "internal": ("Something went wrong on the server.", "See its log."),
    "connection": ("", "Is Clara running, and is CLARA_URL right?"),
    "silent": ("", "The turn was dropped; retry."),
}


def describe_error(code: str, message: str, status: int = 0) -> Described:
    """The line and the next step for an error code. The server's own message is kept when it says more than ours."""
    known = _KNOWN.get(code)
    if known is None:
        return Described(message or f"Clara answered with an error ({status or code}).", "")
    line, step = known
    if not line:
        line = message or code
    elif message and code in ("unavailable",):
        line = message
    return Described(line, step)
