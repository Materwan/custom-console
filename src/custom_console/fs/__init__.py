"""Virtual file system spanning local disks, WSL and the reMarkable tablet."""

from .errors import (
    BinaryFileError,
    InReMarkableError,
    IsVirtualRootError,
    NotAFileError,
    RemarkableError,
    RemarkableUnavailableError,
    UnsafeOperationError,
)
from .manager import Backend, FileManager, Permissions, ReadResult, Target
from .remarkable import RemarkableBackend

__all__ = [
    "Backend",
    "BinaryFileError",
    "FileManager",
    "InReMarkableError",
    "IsVirtualRootError",
    "NotAFileError",
    "Permissions",
    "ReadResult",
    "RemarkableBackend",
    "RemarkableError",
    "RemarkableUnavailableError",
    "Target",
    "UnsafeOperationError",
]
