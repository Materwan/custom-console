"""The Clara client: HTTP and SSE, token reading, event types. Standard library and `httpx` only."""

from .client import DEFAULT_URL, READ_TIMEOUT, ClaraClient
from .errors import ClaraError, ConnectionFailed, StreamSilent, describe_error
from .token import TokenFileError, read_token

__all__ = [
    "DEFAULT_URL", "READ_TIMEOUT", "ClaraClient", "ClaraError", "ConnectionFailed", "StreamSilent", "TokenFileError",
    "describe_error", "read_token",
]
