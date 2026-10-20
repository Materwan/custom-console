"""Linux: xdg-open, `.desktop` files, wl-clipboard / xclip / xsel."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .base import AppMatch, DesktopError, Platform
from .posix import PosixPaths, PosixPrivacy, detach, look_in, restart_in_place, walk_for

PNG = b"\x89PNG\r\n\x1a\n"
FIELD_CODES = re.compile(r"%[a-zA-Z%]")


def applications_dirs(env: dict[str, str] | None = None) -> list[Path]:
    env = dict(os.environ if env is None else env)
    home_data = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    data_dirs = (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [home_data / "applications", *(Path(d) / "applications" for d in data_dirs if d),
            Path("/var/lib/flatpak/exports/share/applications"), home_data / "flatpak" / "exports" / "share" / "applications",
            Path("/var/lib/snapd/desktop/applications")]


def read_desktop_entry(path: Path) -> dict[str, str]:
    """The `[Desktop Entry]` keys of a `.desktop` file (locale-free ones only)."""
    entry: dict[str, str] = {}
    inside = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entry
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            inside = line == "[Desktop Entry]"
        elif inside and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            if "[" not in key:
                entry.setdefault(key.strip(), value.strip())
    return entry


def exec_argv(entry: dict[str, str]) -> list[str]:
    """The command of a desktop entry without its field codes (%u, %F...)."""
    command = FIELD_CODES.sub("", entry.get("Exec", "")).replace("%%", "%")
    return shlex.split(command)


def find_desktop_entry(name: str, folders: list[Path]) -> Path | None:
    wanted = name.lower()
    best: Path | None = None
    for folder in folders:
        try:
            files = sorted(folder.glob("*.desktop"))
        except OSError:
            continue
        for file in files:
            entry = read_desktop_entry(file)
            if entry.get("Type", "Application") != "Application" or entry.get("NoDisplay", "").lower() == "true":
                continue
            names = {entry.get("Name", "").lower(), file.stem.lower(), file.stem.lower().split(".")[-1]}
            if wanted in names:
                return file
            if best is None and wanted in entry.get("Name", "").lower():
                best = file
    return best


class LinuxDesktop:
    def restart_console(self, argv: list[str]) -> None:
        restart_in_place(argv)

    def open_path(self, path: Path) -> None:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise DesktopError("xdg-open is not installed, so nothing can open this file.")
        detach([opener, str(path)])

    def find_app(self, name: str, level: int = 4) -> AppMatch | None:
        home = Path.home()
        if level >= 1 and (found := shutil.which(name)):
            return AppMatch(name, found, 1)
        if level >= 2 and (desktop := find_desktop_entry(name, applications_dirs())):
            return AppMatch(read_desktop_entry(desktop).get("Name", name), str(desktop), 2)
        bins = [Path("/usr/local/bin"), home / ".local" / "bin", home / "bin", Path("/snap/bin"),
                Path("/var/lib/flatpak/exports/bin"), home / ".local" / "share" / "flatpak" / "exports" / "bin"]
        if level >= 3:
            hit = look_in(bins, name) or look_in([Path("/opt"), home / "opt", home / "Applications"], name, one_level_deeper=True)
            if hit:
                return AppMatch(name, str(hit), 3)
        if level >= 4:
            hit = walk_for([Path("/opt"), home / ".local", home / "opt", home / "Applications"], name,
                           skip=frozenset({"node_modules", ".git", "__pycache__", ".cache"}))
            if hit:
                return AppMatch(name, str(hit), 4)
        return None

    def start_app(self, match: AppMatch) -> None:
        if match.target.endswith(".desktop"):
            argv = exec_argv(read_desktop_entry(Path(match.target)))
            if not argv:
                raise DesktopError(f"{match.target} has no command to run.")
            detach(argv)
        else:
            detach([match.target])

    def start_file(self, path: Path) -> None:
        detach([str(path)])

    # ------------------------------------------------------------------ clipboard
    def _tools(self) -> tuple[list[str], list[str], list[str] | None]:
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste") and shutil.which("wl-copy"):
            return ["wl-paste", "--no-newline"], ["wl-copy"], ["wl-paste", "--type", "image/png"]
        if shutil.which("xclip"):
            return (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard"],
                    ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"], None
        raise DesktopError("No clipboard tool found: install wl-clipboard (Wayland) or xclip (X11).")

    def clipboard_read(self) -> str:
        read, _, _ = self._tools()
        try:
            done = subprocess.run(read, capture_output=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopError(f"The clipboard could not be read ({error}).") from None
        if done.returncode != 0:
            return ""  # an empty clipboard makes these tools fail
        return done.stdout.decode("utf-8", errors="replace")

    def clipboard_write(self, text: str) -> None:
        _, write, _ = self._tools()
        try:
            # the tool keeps the selection alive in the background, so its output must not be a pipe we wait on
            subprocess.run(write, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopError(f"The clipboard could not be written ({type(error).__name__}).") from None

    def clipboard_image(self) -> bytes | None:
        _, _, image = self._tools()
        if image is None:
            raise DesktopError("This clipboard tool (xsel) cannot read pictures: install xclip or wl-clipboard.")
        try:
            done = subprocess.run(image, capture_output=True, timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopError(f"The clipboard could not be read ({error}).") from None
        return done.stdout if done.returncode == 0 and done.stdout.startswith(PNG) else None


def make() -> Platform:
    return Platform("linux", PosixPrivacy(), LinuxDesktop(), PosixPaths())
