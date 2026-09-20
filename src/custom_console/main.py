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

from .config import *
from .search_app import find_application, search_path
from .utils.file_utils import *

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from prompt_toolkit import prompt
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory

check_required_paths()

with open(COMMANDS_JSON_PATH, "r") as file:
    COMMANDS = json.load(file)["commands"]

if not os.path.exists(SAVED_APP_PATH):
    with open(SAVED_APP_PATH, "w") as file:
        json.dump({}, file)
with open(SAVED_APP_PATH, "r") as file:
    SAVEDAPP = json.load(file)


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
        self.file_manager = FileManager()

        self.running = True
        self.local_location = os.getcwd()

        self.completer = ShellCompleter(COMMANDS, console_ref=self)

        self.history = InMemoryHistory()

        self.last_used_model = "phi3"

    def load_commands_func(self, commands: Dict) -> Dict[str, Callable[..., Any]]:

        return {name: self.__getattribute__(name) for name in commands.keys()}

    @property
    def location(self):
        return self.file_manager.location

    @property
    def in_remarkable(self):
        return self.file_manager.in_remarkable

    @property
    def remarkable(self):
        return self.file_manager.remarkable

    def cd(self, *args):
        parser = argparse.ArgumentParser(prog="cd", add_help=False, exit_on_error=False)
        parser.add_argument("path", nargs="?", default=".")

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"cd: {e}")
            return

        try:
            self.file_manager.change_directory(parsed.path)
        except FileNotFoundError as e:
            self.console.print(f"cd: {e}: Not found.")
        except NotADirectoryError as e:
            self.console.print(f"cd: {e}: Not as directory")

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

        try:
            self.file_manager.cp(parsed.src, parsed.dst, parsed.recursive)
        except FileNotFoundError as e:
            self.console.print(f"cp: {e}: Not found.")
        except Exception as e:
            self.console.print(f"cp: {e}.")

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

        for path in parsed.paths:
            try:
                res = self.file_manager.stat(path)
                self.console.print(
                    f"[yellow]{self.location} [green]{"r" if res[0] else ""}"
                    f"{"w" if res[1] else ""}{"e" if res[2] else ""}"
                )
            except FileNotFoundError as e:
                self.console.print(f"stat: {e}: Not found.")

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
            with self.console.status(
                f"Searching {parsed.pattern} in {parsed.path} (depth {parsed.depth})..."
            ):
                res = self.file_manager.find(
                    parsed.pattern, parsed.path, parsed.depth, parsed.strict
                )
                if res:
                    self.console.print("\n".join(res))
                else:
                    self.console.print("Pattern not find.")
        except NotADirectoryError:
            self.console.print(f"find: {parsed.path}: Not as directory.")
        except FileNotFoundError:
            self.console.print(f"find: {parsed.path}: Not found.")
        except Exception as e:
            self.console.print(e)

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

        for path in parsed.paths:
            try:
                res = self.file_manager.cat(parsed.paths)
                self.console.print(res)
            except NotAFileError as e:
                self.console.print(f"cat: {e}: Is a directory.")
            except FileNotFoundError as e:
                self.console.print(f"cat: {e}: No such file.")
            except PermissionError as e:
                self.console.print(f"cat: {e}: Permission denied.")

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

        for path in parsed.paths:
            try:
                res = self.file_manager.list(path, parsed.all)
                self.console.print("  ".join(res))
            except NotADirectoryError as e:
                self.console.print(f"ls: {e}: Not a directory.")
            except FileNotFoundError as e:
                self.console.print(f"ls: {e}: Not found.")
            except PermissionError as e:
                self.console.print(f"ls: {e}: Permission denied.")

    def pwd(self, *args):
        parser = argparse.ArgumentParser(
            prog="pwd", add_help=False, exit_on_error=False
        )

        try:
            parsed = parser.parse_args(args)

        except argparse.ArgumentError as e:
            self.console.print(f"stat: {e}")
            return

        self.console.print(self.file_manager.get_working_directory(self))

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
                    os.access(self.location, os.R_OK),
                    os.access(self.location, os.W_OK),
                    os.access(self.location, os.X_OK),
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
