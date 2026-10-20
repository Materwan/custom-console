"""The safety net under every test run.

A test once ran `rm -rf ~` through the shell and deleted a real home folder. Nothing in a test may ever be able to do that
again, so there are independent layers, each enough on its own:

1. `nobody_home` (conftest.py): HOME, USERPROFILE and `Path.home()` are a temp folder for every test.
2. `SandboxedLocalBackend` (harness.py): the file system a test shell or tool works on refuses, before touching anything, to
   write, remove or rename outside its temp folder.
3. This module: for the whole run, `os.remove/unlink/rmdir/removedirs/rename/replace` and `shutil.rmtree` refuse any path that
   is (or contains) a real home, or that is outside the folders tests may change (the temp folder and this repository).
4. `real_home_tripwire` (conftest.py): the run fails if the top level of the real home changed.

And one rule for people: a destructive test never *names* a real path (not `/`, not `/home`, not `~user`). Places that must be
refused are checked with the pure `Vfs.protected_reason`, which removes nothing.

`SandboxViolation` is a BaseException on purpose: no `except Exception` in the code under test can swallow it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = os.path.realpath(Path(__file__).resolve().parents[2])


class SandboxViolation(BaseException):
    """Raised before anything is touched: a test tried to change something it must never change."""


_homes: tuple[str, ...] = ()
_roots: list[str] = []
_installed = False


def primary_home() -> str:
    """The account's own home folder (from the user database on POSIX, which a redirected HOME cannot change)."""
    if sys.platform != "win32":
        import pwd

        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    return os.path.realpath(os.path.expanduser("~"))


def real_homes() -> tuple[str, ...]:
    """Every folder that is somebody's real home, found without trusting HOME (which tests redirect)."""
    found: set[str] = set()
    if sys.platform != "win32":
        import pwd

        found.add(pwd.getpwuid(os.getuid()).pw_dir)
    for name in ("HOME", "USERPROFILE"):
        if os.environ.get(name):
            found.add(os.environ[name])
    found.add(os.path.expanduser("~"))
    return tuple(sorted({os.path.realpath(h) for h in found if h}))


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def allow(path: str | os.PathLike[str]) -> None:
    """Let tests change things under `path` (a temp folder that is not under the system's)."""
    real = os.path.realpath(path)
    if real not in _roots:
        _roots.append(real)


def forbidden(path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
              homes: tuple[str, ...] | None = None, roots: list[str] | None = None) -> str | None:
    """Why a test may not remove or rename `path` (None: it may). Pure: it touches nothing."""
    if isinstance(path, int):
        return None  # a file descriptor: the path was judged when it was opened
    raw = os.fsdecode(os.fspath(path))
    homes = _homes if homes is None else homes
    roots = _roots if roots is None else roots
    for candidate in dict.fromkeys((os.path.abspath(raw), os.path.realpath(raw))):
        for home in homes:
            if _inside(home, candidate):  # the path is the home, or something that holds it
                return f"{candidate} is, or contains, a real home ({home})"
        if not any(_inside(candidate, root) for root in roots):
            return f"{candidate} is outside the folders tests may change"
    return None


def check(path: Any) -> None:
    reason = forbidden(path)
    if reason:
        raise SandboxViolation(f"REFUSED BEFORE ANYTHING WAS TOUCHED: {reason}")


def active() -> bool:
    return _installed


def _guard_one(module: Any, name: str, paths: tuple[tuple[int, str, str | None], ...]) -> None:
    """Wrap `module.name`: each (position, keyword, dir_fd keyword) is a path argument to judge. A path given relative to a
    directory descriptor is skipped: `shutil.rmtree` judges its starting path, and then works through descriptors."""
    original: Callable[..., Any] = getattr(module, name)

    def guarded(*args: Any, **kwargs: Any) -> Any:
        for position, keyword, dir_fd in paths:
            value = args[position] if position < len(args) else kwargs.get(keyword)
            if value is None or (dir_fd is not None and kwargs.get(dir_fd) is not None):
                continue
            check(value)
        return original(*args, **kwargs)

    guarded.__name__ = name
    guarded.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(module, name, guarded)


def install() -> None:
    """Start guarding. Idempotent. Call it before any test code can run (conftest import)."""
    global _homes, _installed
    if _installed:
        return
    _homes = real_homes()
    for root in (tempfile.gettempdir(), REPO, *os.environ.get("CONSOLE_TEST_ALLOW", "").split(os.pathsep)):
        if root:
            allow(root)
    for name in ("remove", "unlink", "rmdir"):
        _guard_one(os, name, ((0, "path", "dir_fd"),))
    _guard_one(os, "removedirs", ((0, "name", None),))
    for name in ("rename", "replace"):
        _guard_one(os, name, ((0, "src", "src_dir_fd"), (1, "dst", "dst_dir_fd")))
    _guard_one(shutil, "rmtree", ((0, "path", "dir_fd"),))
    _installed = True
