"""Locate Windows applications by name (used by the `launch` command)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:  # Windows only
    import winreg
except ImportError:  # pragma: no cover - other platforms
    winreg = None  # type: ignore[assignment]

MAX_LEVEL = 4
StageCallback = Callable[[str], None]


def clean_name(app_name: str) -> str:
    """``" Opera.EXE "`` -> ``"Opera"``."""
    name = app_name.strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _base_directories() -> List[Path]:
    variables = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA")
    return [Path(v) for v in (os.environ.get(name) for name in variables) if v]


# --------------------------------------------------------------------------- #
# Saved applications
# --------------------------------------------------------------------------- #


class SavedApps:
    """Name -> executable path cache, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def names(self) -> List[str]:
        return sorted(self._load())

    def get(self, name: str) -> Optional[str]:
        """Saved path for `name`, or None. A stale entry is forgotten."""
        key = clean_name(name).lower()
        data = self._load()
        path = data.get(key)
        if path is None:
            return None
        if os.path.isfile(path):
            return path
        data.pop(key, None)
        self._save(data)
        return None

    def remember(self, name: str, path: str) -> None:
        data = self._load()
        data[clean_name(name).lower()] = path
        self._save(data)


# --------------------------------------------------------------------------- #
# Search strategies, from the fastest to the slowest
# --------------------------------------------------------------------------- #


def search_path(app_name: str) -> Optional[str]:
    """Look in the directories of the PATH."""
    found = shutil.which(app_name) or shutil.which(app_name + ".exe")
    return os.path.abspath(found) if found else None


def search_registry(app_name: str) -> Optional[str]:
    """Look in the Windows "App Paths" registry keys."""
    if winreg is None:
        return None

    wanted = app_name.lower()
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        ),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]

    for hive, registry_path in locations:
        try:
            with winreg.OpenKey(hive, registry_path) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, index)
                    if Path(subkey_name).stem.lower() != wanted:
                        continue
                    try:
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            exe_path, _ = winreg.QueryValueEx(subkey, "")
                    except OSError:
                        continue
                    if os.path.isfile(exe_path):
                        return os.path.abspath(exe_path)
        except OSError:
            continue
    return None


def search_install_folders(app_name: str) -> Optional[str]:
    """Look for ``<base>/<folder>/<app>.exe`` and ``<base>/<folder>/<sub>/<app>.exe``."""
    exe_name = app_name + ".exe"
    for base in _base_directories():
        try:
            for directory in base.iterdir():
                if not directory.is_dir():
                    continue
                candidate = directory / exe_name
                if candidate.is_file():
                    return str(candidate.resolve())
                try:
                    for subdirectory in directory.iterdir():
                        candidate = subdirectory / exe_name
                        if subdirectory.is_dir() and candidate.is_file():
                            return str(candidate.resolve())
                except OSError:
                    continue
        except OSError:
            continue
    return None


def search_recursive(app_name: str) -> Optional[str]:
    """Walk the whole installation folders. Slow: last resort."""
    wanted = (app_name + ".exe").lower()
    for base in _base_directories():
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.lower() == wanted:
                    return os.path.join(root, name)
    return None


STAGES: List[Tuple[str, Callable[[str], Optional[str]]]] = [
    ("PATH", search_path),
    ("the registry", search_registry),
    ("installation folders", search_install_folders),
    ("installation folders (recursive, slow)", search_recursive),
]


def find_application(
    app_name: str,
    *,
    level: int = MAX_LEVEL,
    saved: Optional[SavedApps] = None,
    on_stage: Optional[StageCallback] = None,
) -> Optional[str]:
    """Path of the executable of `app_name`, or None.

    `level` is the number of search stages to run (1 = PATH only ... 4 = all);
    -1 means all. Saved applications are checked first, and a successful search
    is remembered in `saved`. `on_stage` receives a label before each stage.
    """
    if level == -1:
        level = MAX_LEVEL
    if not 1 <= level <= MAX_LEVEL:
        raise ValueError(f"search level must be between 1 and {MAX_LEVEL} (or -1)")

    name = clean_name(app_name)

    if saved is not None:
        known = saved.get(name)
        if known:
            return known

    for label, search in STAGES[:level]:
        if on_stage is not None:
            on_stage(label)
        found = search(name)
        if found:
            if saved is not None:
                saved.remember(name, found)
            return found
    return None
