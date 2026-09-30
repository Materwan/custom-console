"""Exceptions raised by the file system layer.

The layer is independent of any display concern: callers (the shell, the
agent tools) decide how to present these errors to their own audience.
"""

from __future__ import annotations


class NotAFileError(OSError):
    """A path was expected to be a file but is a directory."""


class BinaryFileError(ValueError):
    """A text operation was attempted on a binary file."""


class InReMarkableError(Exception):
    """The operation makes no sense on reMarkable (e.g. `cat` on a notebook)."""


class RemarkableUnavailableError(RuntimeError):
    """reMarkable was requested but rmapi is not configured."""


class RemarkableError(OSError):
    """rmapi reported an error (rate limit, authentication, quota, ...)."""


class IsVirtualRootError(Exception):
    """The operation makes no sense on the virtual root "/".

    The virtual root is not a real folder, only a menu leading to the drives,
    reMarkable and WSL.
    """


class UnsafeOperationError(Exception):
    """A destructive operation was refused by a safety guard."""
