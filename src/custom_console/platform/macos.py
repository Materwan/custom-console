"""macOS: `open`, `.app` bundles, pbcopy / pbpaste (pngpaste for pictures)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import AppMatch, DesktopError, Platform
from .posix import PosixPaths, PosixPrivacy, detach, look_in, restart_in_place, walk_for

PNG = b"\x89PNG\r\n\x1a\n"


class MacPaths(PosixPaths):
    case_insensitive = True  # the default volume format
    _mac_names = ("Library/Keychains", "Library/Cookies", "Library/Application Support/Google/Chrome",
                  "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
                  "Library/Application Support/Microsoft Edge", "Library/Application Support/Vivaldi")

    def credential_dirs(self, home: Path) -> tuple[Path, ...]:
        return (*super().credential_dirs(home), *(home / name for name in self._mac_names))


def bundles(folders: list[Path]) -> list[Path]:
    found: list[Path] = []
    for folder in folders:
        try:
            found += sorted(p for p in folder.iterdir() if p.suffix == ".app")
        except OSError:
            continue
    return found


class MacDesktop:
    def restart_console(self, argv: list[str]) -> None:
        restart_in_place(argv)

    def open_path(self, path: Path) -> None:
        detach(["open", str(path)])

    def find_app(self, name: str, level: int = 4) -> AppMatch | None:
        home = Path.home()
        wanted = name.lower().removesuffix(".app")
        if level >= 1 and (found := shutil.which(name)):
            return AppMatch(name, found, 1)
        folders = [Path("/Applications"), Path("/System/Applications"), Path("/Applications/Utilities"), home / "Applications"]
        if level >= 2:
            for bundle in bundles(folders):
                if bundle.stem.lower() == wanted:
                    return AppMatch(bundle.stem, str(bundle), 2)
        if level >= 3:
            hit = look_in([Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".local" / "bin"], name)
            if hit:
                return AppMatch(name, str(hit), 3)
        if level >= 4:
            for bundle in bundles([p for f in folders for p in f.glob("*") if p.is_dir() and p.suffix != ".app"]):
                if bundle.stem.lower() == wanted:
                    return AppMatch(bundle.stem, str(bundle), 4)
            hit = walk_for([home / "Applications", Path("/opt")], name, max_depth=5)
            if hit:
                return AppMatch(name, str(hit), 4)
        return None

    def start_app(self, match: AppMatch) -> None:
        if match.target.endswith(".app"):
            detach(["open", "-a", match.target])
        else:
            detach([match.target])

    def start_file(self, path: Path) -> None:
        detach([str(path)])

    def clipboard_read(self) -> str:
        try:
            done = subprocess.run(["pbpaste"], capture_output=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopError(f"The clipboard could not be read ({error}).") from None
        return done.stdout.decode("utf-8", errors="replace")

    def clipboard_write(self, text: str) -> None:
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopError(f"The clipboard could not be written ({type(error).__name__}).") from None

    def clipboard_image(self) -> bytes | None:
        if shutil.which("pngpaste") is None:
            raise DesktopError("Pasting a picture needs `pngpaste` (brew install pngpaste).")
        try:
            done = subprocess.run(["pngpaste", "-"], capture_output=True, timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopError(f"The clipboard could not be read ({error}).") from None
        return done.stdout if done.returncode == 0 and done.stdout.startswith(PNG) else None


def make() -> Platform:
    return Platform("macos", PosixPrivacy(), MacDesktop(), MacPaths())
