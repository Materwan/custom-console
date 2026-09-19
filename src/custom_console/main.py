import os
import sys
import json
import shlex
import subprocess
import ctypes
import requests
import argparse
import time
import posixpath
import shutil
import tempfile
import re

from typing import Dict, List, Callable, Any, Optional, Tuple, Literal

from .search_app import find_application, search_path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from prompt_toolkit import prompt
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory

RMAPI_PATH = os.environ.get(
    "RMAPI_PATH",
    "C:\\Users\\erwan\\Documents\\Programmation\\Prototype\\terminal\\tools\\rmapi.exe",
)

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(SCRIPT_DIR, "utils.json"), "r") as file:
    COMMANDS = json.load(file)["commands"]

saved_app_path = os.path.join(SCRIPT_DIR, "saved_app.json")
if not os.path.exists(saved_app_path):
    with open(saved_app_path, "w") as file:
        json.dump({}, file)
with open(saved_app_path, "r") as file:
    SAVEDAPP = json.load(file)


class RemarkableBackend:
    """
    Wrapper autour de rmapi.exe en mode "one-shot" (`rmapi.exe <cmd> <args>`),
    le seul mode qui fonctionne correctement hors d'un vrai terminal : le mode
    interactif (`rmapi.exe` seul, avec son prompt `[/]>`) utilise une lib
    readline qui a besoin d'un TTY et ne lit pas les commandes envoyées via
    un pipe stdin (d'où le "EOF" immédiat).

    Comme chaque appel au binaire est indépendant (rmapi ne garde aucun état
    entre deux invocations), c'est nous qui suivons le "dossier distant
    courant" côté Python, et qui le recombinons avec le chemin demandé avant
    chaque commande.
    """

    def __init__(self, exe_path: str = RMAPI_PATH):
        self.exe_path = exe_path
        self.path = "/"  # dossier distant courant, à la manière d'un chemin posix

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            [self.exe_path, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.stdout + proc.stderr

    def _resolve(self, subpath: str) -> str:
        """Combine le dossier courant et le chemin demandé (gère '.', '..', '/abs')."""
        if subpath in (".", ""):
            base = self.path
        elif subpath.startswith("/"):
            base = subpath
        else:
            base = posixpath.join(self.path, subpath)
        return posixpath.normpath(base) or "/"

    def _parse_ls(self, output: str) -> List[str]:
        """
        Sortie attendue (mode one-shot), une ligne par entrée :
            [f]     NomDuFichier
            [d]     NomDuDossier
        """
        entries = []
        for line in output.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                if parts[0] == "[f]":
                    entries.append(parts[1].strip())
                if parts[0] == "[d]":
                    entries.append(parts[1].strip() + "/")
        return entries

    def _parse_ls_typed(self, output: str) -> List[Tuple[str, bool]]:
        """Comme _parse_ls mais retourne (nom, est_dossier)."""
        entries = []
        for line in output.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[0] in ("[f]", "[d]"):
                entries.append((parts[1].strip(), parts[0] == "[d]"))
        return entries

    def listdir(self, subpath: str = ".") -> List[str]:
        target = self._resolve(subpath)
        return self._parse_ls(self._run("ls", target))

    def listdir_typed(self, subpath: str = ".") -> List[Tuple[str, bool]]:
        target = self._resolve(subpath)
        return self._parse_ls_typed(self._run("ls", target))

    def cd(self, subpath: str) -> bool:
        target = self._resolve(subpath)
        print(target)
        output = self._run("ls", target)
        if "ERROR" in output:
            # If subpath not a file or a direcory
            return False
        if self._parse_ls(output)[0] == subpath:
            # If subpath is a file
            return False
        self.path = target
        return True

    def get(self, filename: str, dest: str = ".") -> str:
        target = self._resolve(filename)
        return self._run("get", target, dest)

    def put(self, local_path: str, remote_path: str = ".") -> str:
        """Envoie un fichier local vers la tablette."""
        target = self._resolve(remote_path)
        # rmapi.exe put <local_file> <remote_path>
        return self._run("put", local_path, target)

    def stat_entry(self, name: str) -> Optional[str]:
        """rmapi n'a pas de vrai `stat` ; on se contente de dire si
        l'entrée existe dans le dossier courant (fichier ou dossier)."""
        entries = self.listdir(".")
        return name if name in entries else None

    def pwd(self) -> str:
        return self.path


def check_user_answer(answer: str, expected_answer: List[str]):
    return any([answer == expected for expected in expected_answer])


def check_yes_no_answer(answer: str):
    return check_user_answer(answer, ["Y", "y", ""])


class ShellCompleter(Completer):

    def __init__(self, commands: Dict, console_ref: "CustomConsole" = None):
        self.commands = commands
        self.path_executables = self._get_path_executables()
        self.console_ref = console_ref

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # ─────────────────────────────────────────
        # 1. Complétion du nom de commande
        # ─────────────────────────────────────────

        if " " not in text:
            seen = set()
            for command in self.commands:
                if command.startswith(text):
                    seen.add(command)
                    yield Completion(command, start_position=-len(text))

            for exe in self.path_executables:
                if exe.startswith(text) and exe not in seen:
                    yield Completion(exe, start_position=-len(text))
            return

        # ─────────────────────────────────────────
        # 2. Analyse de la commande
        # ─────────────────────────────────────────

        try:
            parts = shlex.split(text)
        except ValueError:
            return

        if not parts:
            return

        command = parts[0]
        config = self.commands.get(command)

        if config is None:
            return

        # Ce qui est actuellement en train d'être complété
        current = parts[-1] if len(parts) > 1 else ""

        # ─────────────────────────────────────────
        # 3. Arguments définis dans le JSON
        # ─────────────────────────────────────────

        for arg in config.get("args", []):
            if arg.startswith(current):
                yield Completion(arg, start_position=-len(current))

        if command == "launch":
            for key in SAVEDAPP:
                yield Completion(key, start_position=-len(current))

        # ─────────────────────────────────────────
        # 4. Complétion des fichiers
        # ─────────────────────────────────────────

        if config.get("files", False):
            yield from self._complete_files(current)

    def _complete_files(self, current):
        """
        Complète les fichiers/dossiers sans parcourir
        récursivement le système de fichiers.
        """

        # Séparer le dossier du nom actuellement tapé
        directory, partial = os.path.split(current)
        partial = partial.lower()

        if self.console_ref and self.console_ref.in_remarkable:
            yield from self._complete_remarkable(directory or ".", partial)
            return

        # Complétion d'un chemin explicite vers reMarkable même si on n'y
        # est pas encore entré, ex: "cd /reMarkable/Epi<TAB>"
        if self.console_ref and directory:
            remote_dir = self.console_ref._strip_remote_prefix(directory)
            if remote_dir is not None:
                abs_remote_dir = (
                    remote_dir if remote_dir.startswith("/") else "/" + remote_dir
                )
                yield from self._complete_remarkable(abs_remote_dir, partial)
                return

        if not directory:
            directory = "."
            if "remarkable".startswith(partial):
                yield Completion("/reMarkable/", -len(partial), display="reMarkable")

        # Gérer ~
        directory = os.path.expanduser(directory)

        # Résoudre le chemin
        search_dir = os.path.abspath(directory)

        try:
            entries = os.listdir(search_dir)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return

        for entry in entries:
            if not entry.lower().startswith(partial):
                continue

            full_path = os.path.join(search_dir, entry)

            # On propose fichiers ET dossiers
            display_name = entry
            completion = f"'{display_name}'" if " " in display_name else display_name

            # Ajouter / aux dossiers
            if os.path.isdir(full_path):
                completion += "/"

            yield Completion(
                completion, start_position=-len(partial), display=display_name
            )

    def _complete_remarkable(self, directory: str, partial: str):
        """Complétion des fichiers/dossiers distants, via rmapi."""
        try:
            entries = self.console_ref.remarkable.listdir_typed(directory)
        except Exception:
            return

        for name, is_dir in entries:
            if not name.lower().startswith(partial):
                continue
            display_name = name + "/" if is_dir else name
            completion = f"'{display_name}'" if " " in display_name else display_name
            yield Completion(
                completion, start_position=-len(partial), display=display_name
            )

    def _get_path_executables(self):
        """Scanne tous les dossiers du PATH et retourne les noms d'exécutables trouvés."""
        executables = set()
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        pathext = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep)

        for directory in path_dirs:
            try:
                for entry in os.listdir(directory):
                    name, ext = os.path.splitext(entry)
                    if ext.upper() in [e.upper() for e in pathext]:
                        executables.add(name)
                    elif not ext:
                        # Certains outils (WSL, git-bash tools) n'ont pas d'extension
                        executables.add(entry)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue

        return executables


class CustomConsole:

    def __init__(self):
        self.command_func = self.load_commands_func(COMMANDS)

        self.console = Console()
        self.console.clear()

        self.running = True

        self.local_location = os.getcwd()
        self.remarkable = RemarkableBackend()
        self.in_remarkable = False

        self.completer = ShellCompleter(COMMANDS, console_ref=self)

        self.history = InMemoryHistory()

        self.last_used_model = "phi3"

    def load_commands_func(self, commands: Dict) -> Dict[str, Callable[..., Any]]:

        return {name: self.__getattribute__(name) for name in commands.keys()}

    @property
    def location(self) -> str:
        if self.in_remarkable:
            return f"reMarkable:{self.remarkable.pwd()}"
        return self.local_location

    def _expand_path(self, path: str) -> str:
        if not self.in_remarkable:
            return os.path.expanduser(path).replace("\\", "/")
        return path.replace("\\", "/")

    def cd(self, *args):
        parser = argparse.ArgumentParser(prog="cd", add_help=False, exit_on_error=False)
        parser.add_argument("path", nargs="?", default=".")

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"cd: {e}")
            return

        target = self._expand_path(parsed.path)

        # ── Cas 0 : "~" ou "~/..." ramène toujours au système de fichiers
        # local, même si on est actuellement dans reMarkable ──
        raw = parsed.path
        if raw == "~" or raw.startswith("~/") or raw.startswith("~\\"):
            home_target = os.path.expanduser(raw).replace("\\", "/")
            self.in_remarkable = False
            try:
                os.chdir(home_target)
            except FileNotFoundError:
                self.console.print(f"cd: {raw}: No such file or directory")
                return
            except NotADirectoryError:
                self.console.print(f"cd: {raw}: Not a directory")
                return
            self.local_location = os.getcwd().replace("\\", "/")
            return

        # ── Cas 1 : on est en local et on entre dans "reMarkable" ──
        # (accepte aussi "/reMarkable/sous/dossier" en un seul cd)
        if not self.in_remarkable:
            remote_target = self._strip_remote_prefix(target)
            if remote_target is not None:
                self.in_remarkable = True
                self.remarkable.path = "/"
                if remote_target not in (".", ""):
                    if not self.remarkable.cd(remote_target):
                        self.console.print(
                            f"cd: {parsed.path}: No such file or directory"
                        )
                        self.in_remarkable = False
                return

        # ── Cas 2 : on est déjà sur la tablette ──
        if self.in_remarkable:
            if not self.remarkable.cd(target):
                self.console.print(f"cd: {target}: No such file or directory")
            return

        # ── Cas 3 : navigation locale classique ──
        try:
            os.chdir(target)
        except FileNotFoundError:
            self.console.print(f"cd: {target}: No such file or directory")
            return
        except NotADirectoryError:
            self.console.print(f"cd: {target}: Not a directory")
            return
        self.local_location = os.getcwd().replace("\\", "/")

    @staticmethod
    def _strip_remote_prefix(path: str) -> Optional[str]:
        """
        Si `path` désigne un chemin sur reMarkable ('reMarkable:xxx',
        '/reMarkable/xxx' ou '/reMarkable'), retourne le chemin distant nu
        (insensible à la casse). Sinon retourne None.
        """
        lowered = path.lower()
        for prefix in ("remarkable:", "/remarkable/", "/remarkable"):
            if lowered.startswith(prefix):
                remainder = path[len(prefix) :]
                return remainder if remainder else "."
        return None

    def cp(self, *args):
        parser = argparse.ArgumentParser(prog="cp", add_help=False, exit_on_error=False)
        parser.add_argument("src")
        parser.add_argument("dst")
        parser.add_argument("-r", "--recursive", action="store_true")

        try:
            parsed = parser.parse_args(args)
        except argparse.ArgumentError as e:
            self.console.print(f"cp: {e}")
            return

        src = parsed.src
        dst = parsed.dst

        # --- CAS 1 : On est sur la tablette (in_remarkable = True) ---
        if self.in_remarkable:
            # Copie interne à la tablette (rmapi ne supporte pas 'cp' direct)
            # Stratégie : get vers un dossier temporaire -> put depuis ce dossier
            self.console.print("cp: Copie interne reMarkable via relais local...")
            tmp_dir = tempfile.mkdtemp(prefix="rmapi_cp_")
            try:
                get_output = self.remarkable.get(src, tmp_dir)
                if "ERROR" in get_output:
                    self.console.print(
                        f"cp: Erreur lors de la récupération de '{src}': {get_output}"
                    )
                    return

                downloaded = os.listdir(tmp_dir)
                if not downloaded:
                    self.console.print(
                        f"cp: Erreur: aucun fichier récupéré pour '{src}'"
                    )
                    return

                local_tmp_file = os.path.join(tmp_dir, downloaded[0])
                put_output = self.remarkable.put(local_tmp_file, dst)
                if "ERROR" in put_output:
                    self.console.print(
                        f"cp: Erreur lors de l'envoi vers '{dst}': {put_output}"
                    )
                    return

                self.console.print(f"[green]Copie réussie: {src} -> {dst}")
            except Exception as e:
                self.console.print(f"cp: Erreur lors de la copie distante: {e}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        # --- CAS 2 : On est en local ---

        # Sous-cas A : Destination est la tablette
        remote_dst = self._strip_remote_prefix(dst)
        if remote_dst is not None:
            local_src = self._expand_path(src)
            result = self.remarkable.put(local_src, remote_dst)
            if "ERROR" in result:
                self.console.print(
                    f"cp: Erreur lors de l'envoi vers reMarkable: {result}"
                )
            else:
                self.console.print(f"[green]Fichier envoyé vers reMarkable: {dst}")
            return

        # Sous-cas B : Source est la tablette
        remote_src = self._strip_remote_prefix(src)
        if remote_src is not None:
            local_dst = self._expand_path(dst)
            result = self.remarkable.get(remote_src, local_dst)
            if "ERROR" in result:
                self.console.print(
                    f"cp: Erreur lors de la récupération depuis reMarkable: {result}"
                )
            else:
                self.console.print(f"[green]Fichier récupéré depuis reMarkable: {dst}")
            return

        # Sous-cas C : Copie locale classique
        src = self._expand_path(src)
        dst = self._expand_path(dst)
        try:
            if os.path.isdir(src):
                if not parsed.recursive:
                    self.console.print(
                        f"cp: -r not specified; omitting directory '{src}'"
                    )
                    return
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            self.console.print(f"[green]Copie locale réussie: {src} -> {dst}")
        except FileNotFoundError:
            self.console.print(f"cp: {src}: No such file or directory")
        except PermissionError:
            self.console.print(f"cp: Permission denied")
        except Exception as e:
            self.console.print(f"cp: {e}")

    def stat(self, *args):
        parser = argparse.ArgumentParser(
            prog="stat", add_help=False, exit_on_error=False
        )
        parser.add_argument("paths", nargs="*", default=["."])

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        if self.in_remarkable:
            for name in parsed.paths:
                found = self.remarkable.stat_entry(name)
                if found:
                    self.console.print(f"[yellow]{name}:[green] existe sur reMarkable")
                else:
                    self.console.print(f"stat: {name} does not exist")
            return

        for file in parsed.paths:

            file_expanded = self._expand_path(file)
            if os.path.isfile(file_expanded) or os.path.isdir(file_expanded):

                read, write, execute = (
                    os.access(file, os.R_OK),
                    os.access(file, os.W_OK),
                    os.access(file, os.X_OK),
                )

                self.console.print(
                    f"[yellow]{file}:[green]{"r" if read else ""}{"w" if write else ""}{"e" if execute else ""}"
                )

            else:

                self.console.print(f"stat: {file} does not exist")

    def _find(self, pattern: str, path: str, depth: int, dir_only: bool, strict: bool):
        if depth == 0:
            return []

        res = []
        try:
            # Compilation du pattern pour optimiser les performances dans la boucle
            # re.IGNORECASE permet de garder le comportement "lower()" actuel
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Si le pattern regex est invalide, on peut soit lever une erreur,
            # soit traiter comme une chaîne littérale
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        try:
            for entry in os.listdir(path):
                full_path = os.path.join(path, entry)

                # Vérification du match avec regex
                # Strict : on vérifie que le pattern correspond exactement à toute la chaîne
                if strict:
                    is_match = bool(regex.fullmatch(entry))
                else:
                    is_match = bool(regex.search(entry))

                if os.path.isdir(full_path):
                    # Si on cherche uniquement des dossiers ET que ça match
                    if dir_only and is_match:
                        res.append(full_path)
                    # Si on cherche des fichiers ou si on doit explorer plus profondément
                    else:
                        # On ajoute le dossier s'il match et qu'on ne cherche pas QUE des dossiers
                        if not dir_only and is_match:
                            res.append(full_path)

                        # Exploration récursive
                        res.extend(
                            self._find(
                                pattern=pattern,
                                path=full_path,
                                depth=depth - 1,
                                dir_only=dir_only,
                                strict=strict,
                            )
                        )
                else:
                    # C'est un fichier : on l'ajoute s'il match et qu'on ne cherche pas QUE des dossiers
                    if not dir_only and is_match:
                        res.append(full_path)
        except PermissionError:
            pass  # Ignorer les dossiers sans permission

        return res

    def find(self, *args) -> str:
        parser = argparse.ArgumentParser(
            prog="find", add_help=False, exit_on_error=False
        )
        parser.add_argument("pattern", nargs="?")
        parser.add_argument("path", nargs="?", default=".")
        parser.add_argument("-d", "--depth", type=int, default=5)
        parser.add_argument("-s", "--strict", action="store_true")

        try:
            parsed = parser.parse_args(args)
        except argparse.ArgumentError as e:
            self.console.print(f"find: {e}")
            return

        try:
            # Détection automatique : si le pattern finit par / ou \ on cherche des dossiers
            is_dir_search = parsed.pattern[-1] in "/\\"
            search_pattern = parsed.pattern[:-1] if is_dir_search else parsed.pattern

            with self.console.status(
                f"Searching {parsed.pattern} in {parsed.path} (depth {parsed.depth})..."
            ):
                res = self._find(
                    pattern=search_pattern,
                    path=parsed.path,
                    depth=parsed.depth,
                    dir_only=is_dir_search,
                    strict=parsed.strict,
                )

            if res:
                # Tri des résultats pour plus de clarté
                self.console.print("\n".join(sorted(res)))
            else:
                self.console.print("No matches found.")
        except Exception as e:
            self.console.print(f"find error: {e}")

    def echo(self, *args):
        parser = argparse.ArgumentParser(
            prog="echo", add_help=False, exit_on_error=False
        )
        parser.add_argument("text", nargs="*", default="")

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.console.print(" ".join(parsed.text))

    def cat(self, *args):
        parser = argparse.ArgumentParser(
            prog="cat", add_help=False, exit_on_error=False
        )
        parser.add_argument("paths", nargs="*", default=["."])

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        if self.in_remarkable:
            self.console.print(
                "cat: impossible d'afficher un fichier reMarkable directement "
                "(ce sont des notebooks, pas du texte). Utilise `get` pour le "
                "télécharger en local d'abord."
            )
            return

        for file in parsed.paths:
            if os.path.isdir(file):
                self.console.print(f"cat: {file}: Is a directory")
            try:
                file_expanded = self._expand_path(file)
                with open(file_expanded) as f:
                    self.console.print(f.read())
            except FileNotFoundError:
                self.console.print(f"cat: {file}: No such file or directory")
            except PermissionError:
                self.console.print(f"cat: {file}: Permission denied")

    def exit(self, *args):
        parser = argparse.ArgumentParser(
            prog="exit", add_help=False, exit_on_error=False
        )

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.running = False

    def ls(self, *args: str) -> int | str:
        parser = argparse.ArgumentParser(prog="ls", add_help=False, exit_on_error=False)
        parser.add_argument("paths", nargs="*", default=["."])
        parser.add_argument("-a", "--all", action="store_true")

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        if self.in_remarkable:
            for folder in parsed.paths:
                entries = self.remarkable.listdir(folder)
                if not parsed.all:
                    entries = [e for e in entries if not e.startswith(".")]
                entries = list(map(lambda x: f"'{x}'" if " " in x else x, entries))
                self.console.print("  ".join(entries))
            return

        for folder in parsed.paths:
            try:
                if not parsed.all:
                    entries = [d for d in os.listdir(folder) if not d.startswith(".")]
                else:
                    folder_expanded = self._expand_path(folder)
                    entries = os.listdir(folder_expanded)
                entries = list(
                    map(
                        lambda x: (
                            x + "/" if os.path.isdir(os.path.join(folder, x)) else x
                        ),
                        entries,
                    )
                )
                entries = list(map(lambda x: f"'{x}'" if " " in x else x, entries))
                self.console.print("  ".join(entries))

            except (FileNotFoundError, OSError):
                self.console.print(
                    f"ls: cannot access '{folder}': No such file or directory"
                )

            except NotADirectoryError:
                self.console.print(folder)

    def pwd(self, *args):
        parser = argparse.ArgumentParser(
            prog="pwd", add_help=False, exit_on_error=False
        )

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.console.print(self.location)

    def clear(self, *args):
        parser = argparse.ArgumentParser(
            prog="clear", add_help=False, exit_on_error=False
        )

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.console.clear()

    def launch(self, *args):
        parser = argparse.ArgumentParser(
            prog="launch", add_help=False, exit_on_error=False
        )
        parser.add_argument("app")
        parser.add_argument("-f", "--file", action="store_true")
        parser.add_argument("-sl", "--search_level", type=int)

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        if not parsed.file:

            search_level = parsed.search_level if parsed.search_level else -1
            if search_level < -1 or search_level > 4:
                self.console.print(
                    "launch: search level must be a number between -1 and 4"
                )
                return

            file = find_application(parsed.app, self.console, search_level)
            if not file:
                self.console.print("launch: executable not found")
                return
        else:
            file = parsed.app

        self.console.print(file)
        try:
            subprocess.Popen(
                [file],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS,
                close_fds=True,
            )
        except FileNotFoundError:
            self.console.print(f"launch: the executable {file} does not exist")

    def reload(self, *args):
        parser = argparse.ArgumentParser(
            prog="reload", add_help=False, exit_on_error=False
        )

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.console.print("Reloading...")
        subprocess.Popen(
            [sys.executable] + sys.argv,
            creationflags=CREATE_NEW_CONSOLE,
        )
        os._exit(0)

    def start_model(self, model: str):

        import ollama
        from .ollama_utils import is_running

        with self.console.status(f"Starting [green]{model}"):
            if is_running(model):
                self.console.print(f"ai: [green]{model} [default]already running")
                return
            else:
                ollama.generate(model=model, prompt="", keep_alive=-1)
        self.console.print(f"[green]{model} [default]started")

    def ai(self, *args):

        from .ollama_utils import (
            get_installed_models,
            get_running_models,
            get_model,
            is_running,
        )

        parser = argparse.ArgumentParser(prog="ai", add_help=False, exit_on_error=False)
        subparsers = parser.add_subparsers(dest="command", required=True)

        # Sous-commande "list"
        list_parser = subparsers.add_parser("list", add_help=False)
        list_parser.add_argument(
            "-r", "--running", action="store_true", help="Filter by running instances"
        )
        list_parser.add_argument(
            "-s", "--size", action="store_true", help="Show size information"
        )
        list_parser.add_argument(
            "-c", "--capabilities", action="store_true", help="Show capabilities"
        )

        # Sous-commande "start" avec un argument optionnel
        start_parser = subparsers.add_parser("start", add_help=False)
        start_parser.add_argument("value", nargs="?", default=None, type=str)

        # Sous-commande "agent"
        agent_parser = subparsers.add_parser("agent", add_help=False)
        agent_parser.add_argument(
            "-n", "--name", nargs="?", default="My Agent", type=str
        )
        agent_parser.add_argument("-m", "--model", default="gemma4", type=str)
        agent_parser.add_argument("-p", "--permissions", default=2, type=int)

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        if parsed.command == "start":
            if not parsed.value:
                use_model = self.last_used_model
            else:
                use_model = get_model(parsed.value)
                if not use_model:
                    self.console.print(f"ai: {parsed.value} model does not exist")
                    return

            self.start_model(use_model)

        elif parsed.command == "list":

            if parsed.running:
                model_list = get_running_models(
                    size=parsed.size, capabilities=parsed.capabilities
                )
                table = Table(title="Running models")
            else:
                model_list = get_installed_models(
                    size=parsed.size, capabilities=parsed.capabilities
                )
                table = Table(title="Installed models")

            table.add_column("Model", style="green", no_wrap=True)

            if parsed.size:
                table.add_column("Size", style="blue")
            if parsed.capabilities:
                capabilites = ["completion", "thinking", "vision", "audio", "tools"]
                for model in model_list:
                    for cap in model["capabilities"]:
                        if not cap in capabilites:
                            capabilites.append(cap)
                for cap in capabilites:
                    table.add_column(cap)

            for model in model_list:
                row = [model["name"]]
                if parsed.size:
                    row.append(str(model["size"]))
                if parsed.capabilities:
                    for cap in capabilites:
                        if cap in model["capabilities"]:
                            row.append("✓")
                        else:
                            row.append("✗")
                table.add_row(*row)

            self.console.print(table)

        elif parsed.command == "agent":

            from .agent.main import AgentConsole

            use_model = get_model(parsed.model)
            if not use_model:
                self.console.print(f"ai: {parsed.model} model does not exist")
                return

            if not is_running(use_model):
                self.console.print(f"ai: {use_model} is not running")
                answer = prompt("Do you want to start it (Y|N) : ")
                if not check_yes_no_answer(answer):
                    return

                self.start_model(use_model)

            agent_console = AgentConsole(
                self.console,
                self.location,
                use_model,
                parsed.name,
                auto_permission_level=parsed.permissions,
            )

            agent_console.run()
            self.console.clear()

    def run(self):

        while self.running:
            if self.in_remarkable:
                self.console.print(f"[yellow]{self.location}")
            else:
                read, write, execute = (
                    os.access(self.local_location, os.R_OK),
                    os.access(self.local_location, os.W_OK),
                    os.access(self.local_location, os.X_OK),
                )
                self.console.print(
                    f"[yellow]{self.location} [green]{"r" if read else ""}{"w" if write else ""}{"e" if execute else ""}"
                )
            instruction = ""
            try:
                instruction = prompt(
                    "> ",
                    completer=self.completer,
                    history=self.history,
                    complete_while_typing=False,
                )
            except KeyboardInterrupt:
                self.console.line()
                continue

            splited = shlex.split(instruction)
            if not splited:
                self.console.line()
                continue

            command, *args = splited
            fn = self.command_func.get(command, None)
            if not fn:
                app = search_path(command)
                if app:
                    subprocess.run([app] + args)
                else:
                    self.console.print(f"{command}: command not found")
                ret = None
            else:
                ret = fn(*args)

            if ret:
                self.console.print(ret)

            if command != "clear":
                self.console.line()


if __name__ == "__main__":

    custom_console = CustomConsole()
    custom_console.run()
    custom_console.console.clear()
