"""What Linux and macOS share: file privacy by mode bits, path rules, detached start, the executable walk."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .base import DesktopError


class PosixPrivacy:
    tightens = True

    def problem(self, path: Path) -> str | None:
        try:
            info = path.stat()
        except OSError as error:
            return f"Cannot check {path}: {error.strerror or error}"
        if info.st_uid != os.getuid():
            return f"{path} belongs to another user."
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            mode = "700" if stat.S_ISDIR(info.st_mode) else "600"
            return f"{path} can be read by others: chmod {mode} {path}"
        return None

    def make_private(self, path: Path) -> None:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)

    def write_private(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


class PosixPaths:
    case_insensitive = False
    _credential_names: tuple[str, ...] = (
        ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".password-store", ".netrc",
        ".config/gcloud", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
        ".config/microsoft-edge", ".config/vivaldi", ".mozilla", ".local/share/keyrings",
    )

    def home(self) -> Path:
        return Path.home()

    def is_network_path(self, raw: str) -> bool:
        return False  # a path starting with // is an ordinary path here

    def is_wsl_path(self, raw: str) -> bool:
        return False

    def credential_dirs(self, home: Path) -> tuple[Path, ...]:
        return tuple(home / name for name in self._credential_names)

    def drives(self) -> list[str]:
        return []

    def wsl_distros(self) -> list[str]:
        return []


def restart_in_place(argv: list[str]) -> None:
    """Replace this process by a fresh console (the terminal, and the prompt, stay where they are)."""
    import sys

    os.execv(sys.executable, [sys.executable, "-m", "custom_console", *argv])


def detach(argv: list[str]) -> None:
    """Start a program that outlives the console, with no input or output of its own."""
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:
        raise DesktopError(f"{argv[0]}: not found") from None
    except PermissionError:
        raise DesktopError(f"{argv[0]}: permission denied") from None
    except OSError as error:
        raise DesktopError(f"{argv[0]}: {error.strerror or error}") from None


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def which(name: str) -> str | None:
    return shutil.which(name)


def look_in(folders: Iterable[Path], name: str, *, one_level_deeper: bool = False) -> Path | None:
    """`<folder>/<name>` and, when asked, `<folder>/<anything>/<name>`; the match ignores case."""
    wanted = name.lower()
    for folder in folders:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.lower() == wanted and is_executable_file(entry):
                return entry
        if one_level_deeper:
            for entry in entries:
                if entry.is_dir():
                    with contextlib.suppress(OSError):
                        for inner in entry.iterdir():
                            if inner.name.lower() == wanted and is_executable_file(inner):
                                return inner
    return None


def walk_for(roots: Iterable[Path], name: str, *, max_depth: int = 6, skip: frozenset[str] = frozenset()) -> Path | None:
    """The slow stage: a walk of whole folders for an executable called `name`."""
    wanted = name.lower()
    for root in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root, onerror=lambda _error: None):
            dirs[:] = [d for d in dirs if d.lower() not in skip and len(Path(current).parts) - base_depth < max_depth]
            for file in files:
                if file.lower() == wanted and is_executable_file(Path(current) / file):
                    return Path(current) / file
    return None
