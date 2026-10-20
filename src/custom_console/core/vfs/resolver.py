"""Turning any typed path into a backend and a checked real path.

The syntax is pure string work (so every Windows form is tested on Linux); the `Resolver` then picks the backend.

Windows has a virtual root `/` listing the drives, the WSL distributions and the reMarkable:

    /                         the virtual root
    /C:/Users/me, C:\\Users\\me   a drive            /wsl-Ubuntu/home/me   a WSL distribution
    /reMarkable/Notes, reMarkable:Notes              the tablet (the second form is relative to its current folder)

On Linux and macOS the real `/` is the root and the tablet is reached only as `reMarkable:path`.
A network path (UNC to another host) is refused outright; WSL's two UNC hosts are not network paths.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...platform import PathRules, Platform
from .backend import Backend, BackendFailure, Entry, PathRefused
from .local import LocalBackend
from .wsl import WslBackend, unc

ROOT, DRIVE, WSL, TABLET, POSIX = "root", "drive", "wsl", "tablet", "posix"
TABLET_NAME = "reMarkable"
DRIVE_FORM = re.compile(r"^([A-Za-z]):(.*)$", re.S)
TABLET_FORM = re.compile(r"^remarkable:(.*)$", re.I | re.S)


@dataclass(frozen=True, slots=True)
class VPath:
    """A parsed virtual path: where it lives, and the segments inside."""

    mount: str
    name: str = ""  # the drive letter or the distribution
    parts: tuple[str, ...] = ()

    def child(self, name: str) -> VPath:
        return VPath(self.mount, self.name, (*self.parts, name))

    @property
    def is_mount_root(self) -> bool:
        return not self.parts


def split(text: str) -> list[str]:
    return [segment for segment in text.split("/") if segment not in ("", ".")]


class Syntax:
    def __init__(self, windows: bool, home: VPath, rules: PathRules, distros: Callable[[], list[str]] = list):
        self.windows = windows
        self.home = home
        self.rules = rules
        self.distros = distros

    # ------------------------------------------------------------------ parsing
    def parse(self, typed: str, cwd: VPath) -> VPath:
        text = typed
        if text == "":
            return cwd
        if text == "~" or text.startswith(("~/", "~\\")):
            return self._walk(self.home, split(text[2:].replace("\\", "/") if self.windows else text[2:]), text)
        tablet = TABLET_FORM.match(text)
        if tablet:
            rest = tablet.group(1).replace("\\", "/") if self.windows else tablet.group(1)
            start = cwd if (cwd.mount == TABLET and not rest.startswith("/")) else VPath(TABLET)
            return self._walk(start, split(rest), text)
        if not self.windows:
            if text.startswith("/"):
                return self._walk(VPath(POSIX), split(text), text)
            return self._walk(cwd if cwd.mount in (POSIX, TABLET) else VPath(POSIX), split(text), text)
        if text.startswith(("\\\\", "//")):
            return self._network(text)
        slashed = text.replace("\\", "/")
        drive = DRIVE_FORM.match(slashed)
        if drive:
            return self._walk(VPath(DRIVE, drive.group(1).upper()), split(drive.group(2)), text)
        if slashed.startswith("/"):
            return self._absolute(split(slashed), cwd, text)
        if cwd.mount == ROOT:
            return self._absolute(split(slashed), cwd, text)
        return self._walk(cwd, split(slashed), text)

    def _absolute(self, segments: list[str], cwd: VPath, typed: str) -> VPath:
        if not segments:
            return VPath(ROOT)
        first, rest = segments[0], segments[1:]
        if re.fullmatch(r"[A-Za-z]:", first):
            return self._walk(VPath(DRIVE, first[0].upper()), rest, typed)
        if first.lower().startswith("wsl-") and len(first) > 4:
            return self._walk(VPath(WSL, self._distro(first[4:])), rest, typed)
        if first.lower() == TABLET_NAME.lower():
            return self._walk(VPath(TABLET), rest, typed)
        if first == "..":
            return VPath(ROOT)
        if cwd.mount == DRIVE:  # `/Users/me` on a drive means the root of that drive
            return self._walk(VPath(DRIVE, cwd.name), segments, typed)
        raise PathRefused(f"{typed}: no such drive or mount (try /, /C:/ or /wsl-<name>/)", typed)

    def _distro(self, name: str) -> str:
        for known in self.distros():
            if known.lower() == name.lower():
                return known
        return name

    def _network(self, text: str) -> VPath:
        backslashed = text.replace("/", "\\")
        if self.rules.is_wsl_path(backslashed):
            pieces = [p for p in backslashed.split("\\") if p]
            if len(pieces) < 2:
                raise PathRefused(f"{text}: name a WSL distribution after the host", text)
            return self._walk(VPath(WSL, self._distro(pieces[1])), pieces[2:], text)
        if backslashed.startswith("\\\\?\\") and not backslashed[4:].lower().startswith("unc\\"):
            return self.parse(backslashed[4:], VPath(ROOT))  # `\\?\C:\x` is a local path in its long form
        raise PathRefused(f"{text}: network paths are not allowed (they could send your credentials to another host)", text)

    def _walk(self, start: VPath, segments: list[str], typed: str) -> VPath:
        current = start
        for index, segment in enumerate(segments):  # `split` has already dropped every "." and empty segment
            if current.mount == ROOT and segment != "..":  # `/C:/../D:/x`: back at the root, the rest names a mount
                return self._absolute(segments[index:], current, typed)
            if segment == "..":
                if current.parts:
                    current = VPath(current.mount, current.name, current.parts[:-1])
                elif self.windows and current.mount in (DRIVE, WSL, TABLET):
                    current = VPath(ROOT)
                continue
            current = current.child(segment)
        return current

    # ------------------------------------------------------------------ forms
    def format(self, path: VPath) -> str:
        tail = "/".join(path.parts)
        if path.mount == ROOT:
            return "/"
        if path.mount == DRIVE:
            return f"/{path.name}:/{tail}"
        if path.mount == WSL:
            return f"/wsl-{path.name}/{tail}"
        if path.mount == TABLET:
            return f"/{TABLET_NAME}/{tail}" if self.windows else f"{TABLET_NAME}:/{tail}"
        return f"/{tail}"

    def inner(self, path: VPath) -> str:
        tail = "/".join(path.parts)
        if path.mount == DRIVE:
            return f"{path.name}:/{tail}"
        if path.mount == WSL:
            return unc(path.name, path.parts)
        if path.mount in (TABLET, POSIX):
            return f"/{tail}"
        return ""

    def from_os_path(self, os_path: str) -> VPath:
        """The virtual path of a real one (the process's working directory, for instance)."""
        if not self.windows:
            return self._walk(VPath(POSIX), split(os_path), os_path)
        try:
            return self.parse(os_path, VPath(ROOT))
        except PathRefused:
            return VPath(ROOT)


@dataclass(frozen=True, slots=True)
class Resolved:
    """A typed path, resolved: its place in the tree, its backend, and its path inside that backend."""

    vpath: VPath
    virtual: str  # canonical, what is shown to people and to the model
    backend: Backend | None  # None for the virtual root
    inner: str  # the backend's own path, as typed after normalising (links not followed)
    typed: str = ""

    @property
    def mount(self) -> str:
        return self.vpath.mount

    @property
    def is_root(self) -> bool:
        return self.vpath.mount == ROOT

    @property
    def on_disk(self) -> bool:
        return self.vpath.mount in (DRIVE, WSL, POSIX)

    def real(self) -> str:
        """The path after following every link, for the sensitive-path checks (SEC-4). Not-yet-existing tails are kept."""
        if not self.on_disk:
            return self.inner
        return os.path.realpath(self.inner)


class Resolver:
    def __init__(self, platform: Platform, *, home: Path | None = None, remarkable: Backend | None = None,
                 local: Backend | None = None):
        self.platform = platform
        self.windows = platform.is_windows
        self.remarkable = remarkable
        self._local = local or LocalBackend()
        self._wsl: dict[str, WslBackend] = {}
        home_path = home if home is not None else platform.paths.home()
        self.home_path = home_path  # the one home: the parser's `~` and the protection of home both read it
        probe = Syntax(self.windows, VPath(ROOT), platform.paths, platform.paths.wsl_distros)
        self.syntax = Syntax(self.windows, probe.from_os_path(str(home_path)), platform.paths, platform.paths.wsl_distros)

    # ------------------------------------------------------------------ resolving
    def start_directory(self) -> str:
        """The virtual path of the process's own working directory."""
        return self.syntax.format(self.syntax.from_os_path(os.getcwd()))

    def parse_virtual(self, text: str) -> VPath:
        return self.syntax.parse(text, VPath(ROOT) if self.windows else VPath(POSIX))

    def resolve(self, typed: str, cwd: str) -> Resolved:
        base = self.parse_virtual(cwd)
        path = self.syntax.parse(typed, base)
        return self.resolved(path, typed)

    def resolved(self, path: VPath, typed: str = "") -> Resolved:
        return Resolved(path, self.syntax.format(path), self.backend_for(path), self.syntax.inner(path), typed)

    def backend_for(self, path: VPath) -> Backend | None:
        if path.mount == ROOT:
            return None
        if path.mount in (DRIVE, POSIX):
            return self._local
        if path.mount == WSL:
            if path.name not in self._wsl:
                self._wsl[path.name] = WslBackend(path.name)
            return self._wsl[path.name]
        if self.remarkable is None:
            raise BackendFailure("The reMarkable is not set up: install rmapi and sign in, or set CONSOLE_RMAPI.")
        return self.remarkable

    # ------------------------------------------------------------------ tree
    def parent(self, resolved: Resolved) -> Resolved:
        return self.resolve("..", resolved.virtual)

    def child(self, resolved: Resolved, name: str) -> Resolved:
        if resolved.is_root:
            return self.resolve(name, resolved.virtual)
        return self.resolved(resolved.vpath.child(name))

    def mounts(self) -> list[Entry]:
        """What the virtual root holds (Windows only)."""
        entries = [Entry(f"{letter}:", True) for letter in self.platform.paths.drives()]
        entries += [Entry(f"wsl-{name}", True) for name in self.platform.paths.wsl_distros()]
        entries.append(Entry(TABLET_NAME, True))
        return entries
