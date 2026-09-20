import os
import shutil
import winreg
import json
from pathlib import Path

from typing import Optional

from custom_console.config import *

from rich.console import Console

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def search_path(app_name: str):

    path = shutil.which(app_name)

    if path:
        return os.path.abspath(path)

    path = shutil.which(app_name + ".exe")

    if path:
        return os.path.abspath(path)


def search_registery(app_name: str):

    app_name_lower = app_name.lower()

    registry_locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        ),
    ]

    for hive, registry_path in registry_locations:

        try:
            with winreg.OpenKey(hive, registry_path) as key:

                number_of_subkeys = winreg.QueryInfoKey(key)[0]

                for i in range(number_of_subkeys):

                    subkey_name = winreg.EnumKey(key, i)

                    # Exemple : "opera.exe" → "opera"
                    name_without_extension = Path(subkey_name).stem

                    if name_without_extension.lower() == app_name_lower:

                        try:
                            with winreg.OpenKey(key, subkey_name) as subkey:

                                exe_path, _ = winreg.QueryValueEx(subkey, "")

                                if os.path.isfile(exe_path):
                                    return os.path.abspath(exe_path)

                        except (FileNotFoundError, PermissionError):
                            pass

        except (FileNotFoundError, PermissionError):
            pass


def search_install_folder(app_name: str):

    base_directories = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
    ]

    for base in base_directories:

        if not base:
            continue

        base = Path(base)

        # On regarde uniquement les dossiers immédiatement
        # présents dans le dossier principal.
        try:

            for directory in base.iterdir():

                if not directory.is_dir():
                    continue

                # Exemple :
                # C:\Program Files\Opera\opera.exe

                candidate = directory / (app_name + ".exe")

                if candidate.is_file():
                    return str(candidate.resolve())

                # Recherche dans un niveau supplémentaire
                # Exemple :
                # Opera\launcher.exe
                # Opera\current\opera.exe

                try:
                    for subdirectory in directory.iterdir():

                        if not subdirectory.is_dir():
                            continue

                        candidate = subdirectory / (app_name + ".exe")

                        if candidate.is_file():
                            return str(candidate.resolve())

                except (PermissionError, OSError):
                    pass

        except (PermissionError, OSError):
            pass


def search_recursive(app_name: str):

    app_name_lower = app_name.lower()

    base_directories = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
    ]

    for base in base_directories:

        if not base:
            continue

        base = Path(base)

        if not base.exists():
            continue

        try:

            for exe in base.rglob("*.exe"):

                if exe.stem.lower() == app_name_lower:
                    print(exe.resolve())
                    print(str(exe.resolve()))
                    return str(exe.resolve())

        except (PermissionError, OSError):
            pass


def find_application(app_name: str, console: Console, level: Optional[int] = -1) -> str:
    """
    Trouve le chemin de l'exécutable d'une application Windows.

    Exemple :
        find_application("Opera")
        find_application("Chrome")
        find_application("Discord")

    Retourne le chemin du .exe ou None.
    """

    level = level if level != -1 else 4

    # ---------------------------------------------------------
    # Nettoyage du nom
    # ---------------------------------------------------------

    app_name = app_name.strip()

    if app_name.lower().endswith(".exe"):
        app_name = app_name[:-4]

    with open(SAVED_APP_PATH, "r") as file:
        dic = json.load(file)
        f = dic.get(app_name.lower())
        if f is not None:
            if os.path.isfile(f):
                return dic[app_name.lower()]
            else:
                dic.pop(app_name)
                with open(SAVED_APP_PATH, "w") as file:
                    file.write(json.dumps(dic, indent="\t"))

    # =========================================================
    # 1. PATH — très rapide
    # =========================================================
    if level > 0:
        with console.status("Searching in PATH..."):

            file = search_path(app_name)

        if file:
            return file

    level -= 1

    # =========================================================
    # 2. REGISTRE WINDOWS — rapide
    # =========================================================
    if level > 0:

        with console.status("Searching in registery..."):

            file = search_registery(app_name)

        if file:
            return file

    level -= 1

    # =========================================================
    # 3. DOSSIERS D'INSTALLATION — assez rapide
    # =========================================================
    if level > 0:

        with console.status("Searching in installation folder..."):

            file = search_registery(app_name)

        if file:
            return file

    level -= 1

    # =========================================================
    # 4. RECHERCHE RÉCURSIVE — lente, dernier recours
    # =========================================================
    if level > 0:

        with console.status("Searching in installation folder recusively..."):

            file = search_recursive(app_name)

        if file:
            return file

    # =========================================================
    # Rien trouvé
    # =========================================================

    return None


if __name__ == "__main__":

    console = Console()

    path = find_application("opera", console)

    if path:
        print("Trouvé :", path)
    else:
        print("Application introuvable")
