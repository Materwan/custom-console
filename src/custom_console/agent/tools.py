import os
import ast
import json
import time
import atexit
import shutil
import requests

from typing import Any, Callable, Dict, Generic, List, Literal, Optional, TypeVar

from .moodle_agent import MoodleAgent
from custom_console.config import *
from custom_console.utils.file_utils import *

from functools import wraps
from dataclasses import dataclass
from rich.markdown import Markdown
from prompt_toolkit import prompt
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def is_yes(answer: str) -> bool:
    """Retourne True si la réponse de l'utilisateur vaut « oui » (vide = oui)."""
    return answer.strip().lower() in YES_ANSWERS


def _load_cache() -> Dict[str, Any]:
    if not os.path.exists(AGENT_CACHE_PATH):
        with open(AGENT_CACHE_PATH, "w") as _:
            pass
    try:
        with open(AGENT_CACHE_PATH, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as e:
        print(e)
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(AGENT_CACHE_PATH, "w", encoding="utf-8") as file:
            file.write(json.dumps(cache, indent="\t"))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Decorator
# --------------------------------------------------------------------------- #


def require_permission(level: int = PERMISSION_LEVEL_READ):
    """
    Décorateur pour gérer les permissions des outils.
    S'assure que la méthode décorée appartient à une classe possédant
    la méthode `ask_permission`.
    """

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            # 1. Générer un message de permission basé sur le nom de la fonction et les arguments
            # On peut essayer d'extraire un message plus propre depuis la docstring si besoin
            func_name = func.__name__.replace("_", " ").title()

            # Tentative de construire un message descriptif
            # On récupère les noms des arguments passés pour rendre le message explicite
            params_str = ""
            if args or kwargs:
                # On crée une chaîne simple : "path='...', pattern='...'"
                import inspect

                sig = inspect.signature(func)
                bound_args = sig.bind(self, *args, **kwargs)
                # On exclut 'self' du message
                relevant_args = {
                    k: v for k, v in bound_args.arguments.items() if k != "self"
                }
                params_str = f" with {relevant_args}"

            permission_msg = f"Agent is trying to {func_name}{params_str}."

            # 2. Appel à la méthode de permission de l'instance (self)
            if not self.ask_permission(permission_msg, level=level):
                return ToolResult.fail(
                    UserPermissionDenied(f"User refused the operation: {func_name}")
                )

            # 3. Exécution de la fonction si permission accordée
            try:
                return func(self, *args, **kwargs)
            except Exception as error:
                return ToolResult.fail(error)

        return wrapper

    return decorator


class UserPermissionDenied(Exception):
    """Levée / retournée quand l'utilisateur refuse une action de l'agent."""


T = TypeVar("T")


@dataclass
class ToolResult(Generic[T]):
    """Résultat standard des outils de l'agent.

    Convention :
    - success is True  -> `data` contient le résultat, `error` vaut None.
    - success is False -> `error` contient l'exception, `data` d'éventuels
      résultats partiels.
    """

    success: bool
    data: Optional[T] = None
    error: Optional[Exception] = None

    @classmethod
    def ok(cls, data: Optional[T] = None) -> "ToolResult[T]":
        return cls(success=True, data=data, error=None)

    @classmethod
    def fail(cls, error: Exception, data: Optional[T] = None) -> "ToolResult[T]":
        """Échec, avec d'éventuels résultats partiels."""
        return cls(success=False, data=data, error=error)

    def is_ok(self) -> bool:
        return self.success

    def is_error(self) -> bool:
        return not self.success

    def unwrap(self) -> T:
        """Retourne `data` si succès, lève une RuntimeError sinon."""
        if not self.success:
            raise RuntimeError(f"ToolResult.unwrap() called on error: {self.error}")
        return self.data  # type: ignore[return-value]

    def to_dict(self) -> Dict[str, Any]:
        """Sérialisation simple, utilisable par l'agent ou les logs.

        Les clés inutiles (`data` absente, `error` absente) sont omises
        plutôt que renvoyées à `null`, pour ne pas gaspiller de tokens à
        chaque appel d'outil (18 outils, potentiellement des dizaines
        d'appels par conversation).
        """
        result: Dict[str, Any] = {"success": self.success}
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = str(self.error)
        return result


class AgentTool:

    def __init__(
        self,
        file_manager: FileManager,
        console,
        live_buffer,
        auto_permission_level: int,
    ):
        self.file_manager = file_manager
        self.console = console
        self.live_buffer = live_buffer
        self.auto_permission_level = auto_permission_level

        self._moodle_agent: Optional[MoodleAgent] = None
        self._moodle_executor: Optional[ThreadPoolExecutor] = None

    def get_agent_tools(self) -> List[Callable[..., ToolResult]]:
        return [
            self.send_email,
            self.get_location,
            self.get_weather,
            self.file_system_pwd,
            self.file_system_list,
            self.file_system_read,
            self.file_system_stat,
            self.file_system_find,
            self.file_system_cd,
            self.file_system_tree,
            self.file_system_copy,
            self.workspace_file_list,
            self.workspace_file_read,
            self.workspace_file_write,
            self.workspace_file_delete,
            self.workspace_file_move,
            self.workspace_file_get,
        ]

    # --------------------------------------------------------------------------- #
    # Helper
    # --------------------------------------------------------------------------- #

    # -- moodle --------------------------------------------------------------- #

    def _ensure_moodle_executor(self) -> ThreadPoolExecutor:
        """Lazily starts (and caches) the dedicated Moodle/Playwright thread.

        Playwright's sync API manipulates the asyncio event loop policy of the
        thread it runs on (it needs a `ProactorEventLoop` on Windows to spawn
        the browser subprocess). If it runs on the main thread, this clashes
        with `prompt_toolkit`'s own internal `asyncio.run()` calls (used by
        `prompt()`), causing a `RuntimeError: asyncio.run() cannot be called
        from a running event loop` on the next prompt.

        To avoid this, every Playwright/Moodle call is executed in a single
        dedicated background thread (one worker, so all calls are naturally
        serialized and the Playwright objects, which are not thread-safe,
        are only ever touched from that one thread). The main thread, where
        `prompt()` runs, is never touched by Playwright.
        """
        if self._moodle_executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moodle")
            self._moodle_executor = executor
            atexit.register(self._shutdown_moodle_executor)
        return self._moodle_executor

    def _shutdown_moodle_executor(self) -> None:
        """Closes the `MoodleAgent` and stops its dedicated thread, if any."""
        executor = self._moodle_executor
        if executor is None:
            return
        try:
            executor.submit(self._close_moodle_agent).result(timeout=30)
        except Exception:
            pass
        executor.shutdown(wait=True)
        self._moodle_executor = None

    def _close_moodle_agent(self) -> None:
        """Closes the underlying `MoodleAgent`. Must run on the Moodle thread."""
        if self._moodle_agent is not None:
            self._moodle_agent.close()
            self._moodle_agent = None

    def _get_moodle_agent(self) -> MoodleAgent:
        """Starts (once) and returns the `MoodleAgent`. Must run on the Moodle thread."""
        if self._moodle_agent is None:
            moodle_agent = MoodleAgent(state_path=MOODLE_COOKIE_PATH)
            moodle_agent.start()
            self._moodle_agent = moodle_agent
        return self._moodle_agent

    def _run_on_moodle_thread(self, fn: Callable[[MoodleAgent], T]) -> T:
        """Runs `fn(moodle_agent)` on the dedicated Moodle/Playwright thread.

        `fn` receives the started `MoodleAgent` instance and its return value
        is passed back to the caller (on the calling thread). Any exception
        raised inside `fn` is re-raised here, on the caller's thread.
        """
        executor = self._ensure_moodle_executor()

        def job() -> T:
            moodle_agent = self._get_moodle_agent()
            return fn(moodle_agent)

        return executor.submit(job).result()

    # -- affichage ---------------------------------------------------------- #

    def live_print(self, text: str) -> None:
        """Écrit dans le buffer si un rendu Live est en cours, sinon direct."""
        if self.live_buffer is not None:
            self.live_buffer.append(text, force=True)
        else:
            self.console.print(text)

    # -- permissions -------------------------------------------------------- #

    def ask_permission(self, info: str, level: int = PERMISSION_LEVEL_READ) -> bool:
        """Pose une question oui/non pour autoriser une action de l'agent.

        Args:
            info: description de l'action, affichée à l'utilisateur.
            level: niveau de risque requis par l'outil qui appelle cette
                méthode (voir `PERMISSION_LEVEL_*`). Si le niveau
                d'autorisation automatique de la console
                (`self.auto_permission_level`) est supérieur ou égal à
                `level`, la permission est accordée automatiquement, sans
                interrompre l'utilisateur.

        La question et la réponse (ou l'auto-autorisation) sont ajoutées au
        buffer d'affichage : elles restent donc visibles dans l'historique de
        la réponse en cours.
        """
        if self.auto_permission_level >= level:
            record = self._format_permission(info, granted=True, auto=True)
            self.live_print(record)
            return True

        live = self.live_buffer

        if live is not None:
            live.pause()

        granted = self._prompt_permission(info)
        record = self._format_permission(info, granted)

        if live is not None:
            live.append(record, force=True)
            live.resume()
        else:
            self.console.print(Markdown(record))

        return granted

    def _prompt_permission(self, info: str) -> bool:
        """Affiche la demande, lit la réponse, puis efface la zone du terminal.

        Le contenu n'est pas perdu : il est réinjecté dans le buffer par
        `ask_permission`.
        """
        with self.console.capture() as capture:
            self.console.line()
            self.console.print(info, markup=False)
            self.console.line()
        output = capture.get()

        self.console.file.write(output)
        self.console.file.flush()

        try:
            answer = prompt(PERMISSION_PROMPT)
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        # +1 pour la ligne du prompt elle-même.
        lines = output.count("\n") + 1
        if self.console.is_terminal:
            self.console.file.write(f"\x1b[{lines}A")  # remonte le curseur
            self.console.file.write("\x1b[0J")  # efface tout ce qui suit
            self.console.file.flush()

        return is_yes(answer)

    @staticmethod
    def _format_permission(info: str, granted: bool, auto: bool = False) -> str:
        """Met en forme la demande de permission pour le buffer markdown."""
        if auto:
            status = "auto-accepted"
        else:
            status = "accepted" if granted else "refused"
        quoted = "\n".join(f"> {line}" for line in info.splitlines() or [""])
        return f"\n\n{quoted}\n>\n> **Permission {status}.**\n\n"

    # -- other -------------------------------------------------------- #

    @staticmethod
    def _resolve_workspace_path(directory: str, relative_path: str) -> str:
        """Résout `relative_path` à l'intérieur du dossier workspace `directory`.

        Lève `ValueError` si `directory` n'est pas "result"/"tmp", ou si le
        chemin résolu (une fois les ".." et liens symboliques suivis) sort du
        dossier racine correspondant. C'est cette vérification qui garantit
        que l'agent ne peut pas s'échapper du bac à sable, même avec un
        chemin du type "../../etc/passwd" ou un lien symbolique piégé.
        """
        if directory not in WORKSPACE_ROOTS:
            raise ValueError(
                f"Unknown workspace directory {directory!r}; expected one of "
                f"{sorted(WORKSPACE_ROOTS)}."
            )
        root = Path(WORKSPACE_ROOTS[directory]).resolve()
        root.mkdir(parents=True, exist_ok=True)

        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(
                f"{relative_path!r} escapes the {directory!r} workspace directory."
            )
        return str(candidate)

    # -- Compact tool output -------------------------------------------------------- #

    @staticmethod
    def _compact_course_structure(data: Dict[str, Any]) -> str:
        """Condense la structure d'un cours en texte plutôt qu'en JSON imbriqué.

        Le JSON (dicts de dicts avec 'title'/'url'/'due_date'/'kind' répétés
        pour chaque ressource) est plus verbeux que nécessaire pour un LLM.
        Une ligne texte par ressource transporte la même information avec
        nettement moins de tokens.
        """
        lines = [f"Course {data.get('course_id')} — {data.get('url')}"]
        for section in data.get("sections", []):
            lines.append(f"## {section.get('title')}")
            for res in section.get("resources", []):
                due = f" (due: {res['due_date']})" if res.get("due_date") else ""
                lines.append(
                    f"- [{res.get('kind')}] {res.get('title')}{due} — {res.get('url')}"
                )
        return "\n".join(lines)

    @staticmethod
    def _compact_weather(payload: Dict[str, Any], resolution: str) -> Dict[str, Any]:
        """Réduit la réponse Open-Meteo à l'essentiel.

        La réponse brute contient des métadonnées (unités détaillées,
        elevation, etc.) et, en mode "hourly"/"daily", un tableau par
        variable pouvant représenter des centaines de valeurs. On ne garde
        que la section correspondant à `resolution`, avec les floats
        arrondis à 1 décimale, ce qui divise fortement la taille renvoyée
        au modèle sans perte d'information utile.
        """
        block = payload.get(resolution)
        if block is None:
            return payload  # format inattendu, on ne filtre pas au hasard

        def _round(value: Any) -> Any:
            return round(value, 1) if isinstance(value, float) else value

        cleaned = {
            key: [_round(v) for v in val] if isinstance(val, list) else _round(val)
            for key, val in block.items()
        }
        return {resolution: cleaned, "timezone": payload.get("timezone")}

    @staticmethod
    def _get_python_summary(content):
        """
        Analyse le contenu d'un fichier Python et en extrait la structure.
        """
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return "Erreur de syntaxe : Impossible d'analyser le fichier Python."

        summary = []

        for node in tree.body:
            # Extraction des Classes
            if isinstance(node, ast.ClassDef):
                summary.append(f"Class {node.name}:")
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        args = [a.arg for a in item.args.args]
                        summary.append(f"  - def {item.name}({', '.join(args)}): ...")

            # Extraction des Fonctions globales
            elif isinstance(node, ast.FunctionDef):
                args = [a.arg for a in node.args.args]
                summary.append(f"def {node.name}({', '.join(args)}): ...")

            # Extraction des Imports (Optionnel mais utile pour le contexte)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                summary.append(f"# Import: {ast.dump(node)}")

        return "\n".join(summary)

    # --------------------------------------------------------------------------- #
    # Files
    # --------------------------------------------------------------------------- #

    @require_permission(level=PERMISSION_LEVEL_NONE)
    def file_system_pwd(self) -> ToolResult[Any]:
        """Return the current working directory."""
        try:
            return ToolResult.ok(self.file_manager.location)
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_list(
        self,
        path: str = ".",
        all: bool = False,
    ) -> ToolResult[Any]:
        """List directory contents."""
        try:
            return ToolResult.ok(self.file_manager.list(path, all))
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_read(
        self,
        path: str,
        mode: Literal["range", "summary", "full"] = "summary",
        start_line=None,
        end_line=None,
    ) -> ToolResult[str]:
        """
        Version optimisée de l'outil de lecture de fichier.
        """
        content = self.file_manager.cat(path)
        lines = content.split("\n")

        # MODE : Lecture partielle (Range)
        if mode == "range":
            if start_line is None or end_line is None:
                return ToolResult.fail(
                    ValueError(
                        "Missing start_line and end_line required for range mode."
                    )
                )
            return ToolResult.ok("".join(lines[start_line - 1 : end_line]))

        # MODE : Résumé (Summary)
        if mode == "summary":
            if path.endswith(".py"):
                return ToolResult.ok(self._get_python_summary(content))
            else:
                return ToolResult.fail(
                    NotImplementedError(
                        "Summary mode not yet implemented for other extension than .py."
                    )
                )

        # MODE : Complet (Full)
        return ToolResult.ok(content)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_stat(
        self,
        path: str,
    ) -> ToolResult[Any]:
        """Return file permissions."""
        try:
            return ToolResult.ok(self.file_manager.stat(path))
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_find(
        self,
        pattern: str,
        path: str = ".",
        depth: int = 2,
        strict: bool = False,
    ) -> ToolResult[Any]:
        """Find files matching a pattern."""
        try:
            return ToolResult.ok(self.file_manager.find(pattern, path, depth, strict))
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_cd(
        self,
        path: str,
    ) -> ToolResult[Any]:
        """Change the current working directory."""
        try:
            self.file_manager.change_directory(path)
            return ToolResult.ok(f"Changed directory to {self.file_manager.location}")
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_READ)
    def file_system_tree(
        self,
        path: str = ".",
        depth: int = 2,
        all: bool = False,
    ) -> ToolResult[Any]:
        """Show a directory tree."""
        try:
            res = []
            for t in self.file_manager.tree(path, depth, all):
                res.append(t)
            return ToolResult.ok("\n".join(res))
        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def file_system_copy(
        self,
        src: str,
        path: str,
        recursive: bool = False,
    ) -> ToolResult[Any]:
        """Copy a file or directory."""
        try:
            for _ in self.file_manager.copy(src, path, recursive):
                pass
            return ToolResult.ok(f"Copy from {src} to {path} succeed.")
        except Exception as error:
            return ToolResult.fail(error)

    def file_system_op(
        self,
        action: Literal["pwd", "list", "read", "stat", "find", "cd", "tree", "cp"],
        params: Optional[Dict[str, Any]] = None,
        path: str = ".",
        src: str = ".",
        pattern: Optional[str] = None,
        depth: int = 2,
        all: bool = False,
        strict: bool = False,
        recursive: bool = False,
    ) -> ToolResult[Any]:
        """
        General file system operations across local, reMarkable, and virtual root.
        Execute command from working directory, accessible by the action "pwd".

        Args:
            action: "pwd" (get working directory) "list" (ls), "read" (cat), "stat" (permissions), "find" (search), "cd" (change dir),
                "tree" (folder structure), "cp" (copy).
            path: target path.
            src: source path (for "cp").
            pattern: regex pattern (required for "find").
            depth: search depth (for "find" or "tree").
            all: display hidden files/folder (for "ls" and "tree").
            strict: exact match (for "find").
            recursive: if copy is recursive (for "cp").
        """
        # 1. Gestion des permissions
        permission_msg = f"Agent is trying to perform {action} on {path}."
        if action in ("pwd"):
            premission_level = PERMISSION_LEVEL_NONE
        elif action in ("list", "read", "stat", "find", "cd", "tree"):
            premission_level = PERMISSION_LEVEL_READ
        else:
            premission_level = PERMISSION_LEVEL_WRITE
        granted = self.ask_permission(permission_msg, level=premission_level)
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("User refused the file system operation.")
            )

        try:
            if action == "pwd":
                return ToolResult.ok(self.file_manager.location)

            elif action == "list":
                # Retourne la liste des fichiers/dossiers
                return ToolResult.ok(self.file_manager.list(path, all))

            elif action == "read":
                # Lit le contenu d'un fichier
                return ToolResult.ok(self.file_manager.cat(path))

            elif action == "stat":
                # Retourne [readable, writable, executable]
                return ToolResult.ok(self.file_manager.stat(path))

            elif action == "find":
                if not pattern:
                    return ToolResult.fail(
                        ValueError("Pattern is required for 'find' action.")
                    )
                # Recherche récursive
                return ToolResult.ok(
                    self.file_manager.find(pattern, path, depth, strict)
                )

            elif action == "cd":
                # Change le répertoire courant du FileManager
                self.file_manager.change_directory(path)
                return ToolResult.ok(
                    f"Changed directory to {self.file_manager.location}"
                )

            elif action == "tree":
                return ToolResult.ok(
                    "\n".join(
                        [line for line in self.file_manager.tree(path, depth, all)]
                    )
                )

            elif action == "cp":
                self.file_manager.copy(src, path, recursive)
                return ToolResult.ok(f"Copy from {src} to {path} succeed.")

            else:
                return ToolResult.fail(ValueError(f"Unknown action {action}"))

        except Exception as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_list(
        self,
        directory: str,
        path: str = ".",
    ) -> ToolResult[Any]:
        """List files in a workspace directory."""
        try:
            src = self._resolve_workspace_path(directory, path)
        except ValueError as error:
            return ToolResult.fail(error)

        if not os.path.isdir(src):
            return ToolResult.fail(NotADirectoryError(f"{path} is not a directory."))

        try:
            return ToolResult.ok(self.file_manager.list(src, True))
        except OSError as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_read(
        self,
        directory: str,
        path: str,
        encoding: str = "utf-8",
    ) -> ToolResult[Any]:
        """Read a workspace file."""
        try:
            src = self._resolve_workspace_path(directory, path)
        except ValueError as error:
            return ToolResult.fail(error)

        if os.path.isdir(src):
            return ToolResult.fail(IsADirectoryError(f"{path} is a directory."))

        if not os.path.isfile(src):
            return ToolResult.fail(FileNotFoundError(f"{path} is not a file."))

        try:
            return ToolResult.ok(self.file_manager.cat(src))
        except (OSError, UnicodeDecodeError) as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_write(
        self,
        directory: str,
        path: str,
        content: str,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> ToolResult[Any]:
        """Write a file in a workspace."""
        try:
            src = self._resolve_workspace_path(directory, path)
        except ValueError as error:
            return ToolResult.fail(error)

        if not overwrite and os.path.exists(src):
            return ToolResult.fail(FileExistsError(f"{path} already exists."))

        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "w", encoding=encoding) as file:
                file.write(content)
            return ToolResult.ok(src)
        except OSError as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_delete(
        self,
        directory: str,
        path: str,
    ) -> ToolResult[Any]:
        """Delete a file or directory from a workspace."""
        if os.path.normpath(path) in (".", ""):
            return ToolResult.fail(
                ValueError("Refusing to delete the workspace root itself.")
            )

        try:
            src = self._resolve_workspace_path(directory, path)
        except ValueError as error:
            return ToolResult.fail(error)

        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.rmtree(src)
            elif os.path.exists(src):
                os.remove(src)
            else:
                return ToolResult.fail(FileNotFoundError(f"{path} does not exist."))

            return ToolResult.ok(src)
        except OSError as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_move(
        self,
        directory: str,
        path: str,
        dst_directory: str,
        dst_path: str,
    ) -> ToolResult[Any]:
        """Move a file or directory to workspaces."""
        try:
            src = self._resolve_workspace_path(directory, path)
            destination = self._resolve_workspace_path(
                dst_directory,
                dst_path,
            )
        except ValueError as error:
            return ToolResult.fail(error)

        if not os.path.exists(src):
            return ToolResult.fail(FileNotFoundError(f"{path} does not exist."))

        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(src, destination)
            return ToolResult.ok(destination)
        except OSError as error:
            return ToolResult.fail(error)

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def workspace_file_get(
        self,
        src: str,
        dst_directory: str,
    ):
        """Get a file or folder from outside wokspace in to the workspace."""
        try:
            destination = self._resolve_workspace_path(
                dst_directory,
                ".",
            )
        except ValueError as error:
            return ToolResult.fail(error)

        try:
            for _ in self.file_manager.copy(src, destination, recursive=False):
                pass
            return ToolResult.ok(f"Copied {src} to {destination}")
        except OSError as error:
            return ToolResult.fail(error)

    def workspace_file_op(
        self,
        action: Literal["list", "read", "write", "delete", "move"],
        directory: str,
        path: str = ".",
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = True,
        dst_directory: Optional[str] = None,
        dst_path: Optional[str] = None,
    ) -> ToolResult[Any]:
        """File ops (list/read/write/delete/move) sandboxed to "result"/"tmp" workspaces,
        for outside file, use `file_system_op` instead.

        Args:
            action: "list"/"read"/"write"/"delete"/"move".
            directory: "result" or "tmp" (source workspace).
            path: path relative to `directory` (source path for "move").
            content: text to write (action="write" only).
            encoding: text encoding for "read"/"write".
            overwrite: if False, "write" fails instead of overwriting.
            dst_directory, dst_path: destination workspace/path, required for "move".

        Returns ToolResult.data: list[str] for "list", str content for "read",
        else the resulting absolute path.
        """
        try:
            src = self._resolve_workspace_path(directory, path)
        except ValueError as error:
            return ToolResult.fail(error)

        if action == "list":
            granted = self.ask_permission(
                f"Agent is trying to list files in {directory}/{path}.",
                level=PERMISSION_LEVEL_READ,
            )
            if not granted:
                return ToolResult.fail(
                    UserPermissionDenied(
                        "The user refused to list the workspace files."
                    )
                )
            if not os.path.isdir(src):
                return ToolResult.fail(
                    NotADirectoryError(f"{path} is not a directory.")
                )
            try:
                return ToolResult.ok(sorted(os.listdir(src)))
            except OSError as error:
                return ToolResult.fail(error)

        if action == "read":
            granted = self.ask_permission(
                f"Agent is trying to read {directory}/{path}.",
                level=PERMISSION_LEVEL_READ,
            )
            if not granted:
                return ToolResult.fail(
                    UserPermissionDenied("The user refused to give access to the file.")
                )
            if os.path.isdir(src):
                return ToolResult.fail(IsADirectoryError(f"{path} is a directory."))
            if not os.path.isfile(src):
                return ToolResult.fail(FileNotFoundError(f"{path} is not a file."))
            try:
                with open(src, "r", encoding=encoding) as file:
                    return ToolResult.ok(file.read())
            except (OSError, UnicodeDecodeError) as error:
                return ToolResult.fail(error)

        if action == "write":
            granted = self.ask_permission(
                f"Agent is trying to write {directory}/{path}.",
                level=PERMISSION_LEVEL_WRITE,
            )
            if not granted:
                return ToolResult.fail(
                    UserPermissionDenied("The user refused to write the file.")
                )
            if not overwrite and os.path.exists(src):
                return ToolResult.fail(FileExistsError(f"{path} already exists."))
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                with open(src, "w", encoding=encoding) as file:
                    file.write(content)
                return ToolResult.ok(src)
            except OSError as error:
                return ToolResult.fail(error)

        if action == "delete":
            if os.path.normpath(path) in (".", ""):
                return ToolResult.fail(
                    ValueError("Refusing to delete the workspace root itself.")
                )
            granted = self.ask_permission(
                f"Agent is trying to delete {directory}/{path}.",
                level=PERMISSION_LEVEL_WRITE,
            )
            if not granted:
                return ToolResult.fail(
                    UserPermissionDenied("The user refused to delete the file.")
                )
            try:
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.rmtree(src)
                elif os.path.exists(src):
                    os.remove(src)
                else:
                    return ToolResult.fail(FileNotFoundError(f"{path} does not exist."))
                return ToolResult.ok(src)
            except OSError as error:
                return ToolResult.fail(error)

        if action == "move":
            if not dst_directory or dst_path is None:
                return ToolResult.fail(
                    ValueError("dst_directory and dst_path are required for 'move'.")
                )
            try:
                destination = self._resolve_workspace_path(dst_directory, dst_path)
            except ValueError as error:
                return ToolResult.fail(error)
            granted = self.ask_permission(
                f"Agent is trying to move {directory}/{path} to "
                f"{dst_directory}/{dst_path}.",
                level=PERMISSION_LEVEL_WRITE,
            )
            if not granted:
                return ToolResult.fail(
                    UserPermissionDenied("The user refused to move the file.")
                )
            if not os.path.exists(src):
                return ToolResult.fail(FileNotFoundError(f"{path} does not exist."))
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(src, destination)
                return ToolResult.ok(destination)
            except OSError as error:
                return ToolResult.fail(error)

        return ToolResult.fail(ValueError(f"Unknown action {action!r}."))

    # --------------------------------------------------------------------------- #
    # Moodle
    # --------------------------------------------------------------------------- #

    def moodle_get_page_content(
        self,
        url: str,
        selector: Optional[str] = None,
        include_html: bool = False,
        max_chars: Optional[int] = 8000,
    ) -> ToolResult[str]:
        """Navigate to a Moodle URL, return cleaned text (or HTML) of an area.

        By default targets Moodle's main content region (not `<body>`),
        stripping nav/side blocks/scripts, and returns plain text unless
        `include_html`.

        Args:
            url: absolute or relative to Moodle base (e.g. "/my/").
            selector: CSS or XPath ("xpath=", "//", ".."); defaults to main
                content region.
            include_html: return cleaned inner HTML instead of text.
            max_chars: truncate result (default 8000, None to disable);
                prefer a narrower `selector` over raising this.

        Returns ToolResult.data (str): extracted text/HTML. If the URL is a
        non-HTML file (e.g. PDF), an explanatory message telling you to use
        `moodle_download_file` instead — don't retry this tool on it.
        """
        granted = self.ask_permission(
            f"Agent is trying to open the Moodle page {url!r}.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to open the Moodle page.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.get_page_content(
                    url,
                    selector=selector,
                    include_html=include_html,
                    max_chars=max_chars,
                )
            )
            return ToolResult.ok(data)
        except Exception as error:  # Playwright / navigation errors
            return ToolResult.fail(error)

    def moodle_click_element(
        self, selector: str, wait_until: str = "domcontentloaded"
    ) -> ToolResult[Dict[str, Any]]:
        """Click an element on the current Moodle page.

        Args:
            selector: CSS or XPath ("xpath=", "//", "..").
            wait_until: Playwright wait condition (e.g. "domcontentloaded",
                "load", "networkidle").

        Returns ToolResult.data: dict with 'clicked' (bool), 'url', 'title'.
        """
        granted = self.ask_permission(
            f"Agent is trying to click on {selector!r} on the current Moodle page.",
            level=PERMISSION_LEVEL_WRITE,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to click the element.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.click_element(selector, wait_until=wait_until)
            )
            return ToolResult.ok(data)
        except Exception as error:
            return ToolResult.fail(error)

    def moodle_input_text(
        self, selector: str, text: str, submit: bool = False
    ) -> ToolResult[Dict[str, Any]]:
        """Fill a text field on the current Moodle page.

        Args:
            selector: CSS or XPath ("xpath=", "//", "..").
            text: text to type.
            submit: if True, press Enter and wait for navigation.

        Returns ToolResult.data: dict with 'filled' (bool), 'url'.
        """
        granted = self.ask_permission(
            f"Agent is trying to fill {selector!r} with {text!r}"
            f"{' and submit it' if submit else ''} on the current Moodle page.",
            level=PERMISSION_LEVEL_WRITE,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to fill the field.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.input_text(selector, text, submit=submit)
            )
            return ToolResult.ok(data)
        except Exception as error:
            return ToolResult.fail(error)

    def moodle_list_courses(
        self, force_refresh: bool
    ) -> ToolResult[List[Dict[str, Any]]]:
        """List every Moodle course visible to the user, with its numeric id.

        Always call this first to resolve a course id from its name before
        any tool needing a course_id: never guess/scrape ids, use the 'id'
        returned here.

        Args:
            force_refresh: bypass cache.

        Returns ToolResult.data: list[dict] with 'id', 'title', 'url'.
        """
        granted = self.ask_permission(
            "Agent is trying to list your Moodle courses.",
            level=PERMISSION_LEVEL_READ,
        )

        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to list the courses.")
            )

        cache = _load_cache()
        entry = cache.get("courses")

        if not force_refresh and entry:
            return ToolResult.ok(entry["data"])

        try:
            data = self._run_on_moodle_thread(lambda moodle: moodle.list_courses())
        except Exception as error:
            return ToolResult.fail(error)

        cache["courses"] = {"fetched_at": time.time(), "data": data}
        _save_cache(cache)
        return ToolResult.ok(data)

    def moodle_get_course_structure(self, course_id: str) -> ToolResult[Dict[str, Any]]:
        """Extract sections, resources and visible due dates of a course.

        Args:
            course_id: Moodle numeric course id (from URL, e.g. "123").
                Get it via `moodle_list_courses`, never guess it.

        Returns ToolResult.data (str): text with 'course_id', 'url', and per
        section a 'title' with its resources ('title', 'url', 'due_date', 'kind').
        """
        granted = self.ask_permission(
            f"Agent is trying to read the structure of Moodle course {course_id!r}.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to read the course structure.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.get_course_structure(course_id)
            )
            return ToolResult.ok(self._compact_course_structure(data))
        except Exception as error:
            return ToolResult.fail(error)

    def moodle_get_announcements(self, limit: int = 20) -> ToolResult[str]:
        """Fetch announcements on the Moodle dashboard.

        Args:
            limit: max number of announcements.

        Returns compact text: one block per announcement (title, url, text).
        """
        granted = self.ask_permission(
            "Agent is trying to read your Moodle announcements.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to read the announcements.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.get_announcements(limit=limit)
            )
            lines = []
            for item in data:
                lines.append(f"### {item.get('title')} — {item.get('url')}")
                lines.append(item.get("text", ""))
            return ToolResult.ok("\n".join(lines))
        except Exception as error:
            return ToolResult.fail(error)

    def moodle_get_grades(self) -> ToolResult[str]:
        """Extract the rows of the Moodle grade overview accessible to the user.

        Returns text: one pipe-separated line of cells per row.
        """
        granted = self.ask_permission(
            "Agent is trying to read your Moodle grades.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to read the grades.")
            )

        try:
            data = self._run_on_moodle_thread(lambda moodle: moodle.get_grades())
            lines = [" | ".join(row.get("columns", [])) for row in data]
            return ToolResult.ok("\n".join(lines))
        except Exception as error:
            return ToolResult.fail(error)

    def moodle_download_file(
        self, file_url: str, save_directory: str, save_relative_path: str
    ) -> ToolResult[Dict[str, Any]]:
        """Download a file from Moodle using the active session.

        Args:
            file_url: URL to download, absolute or relative to Moodle base.
            save_directory: "result" or "tmp" workspace ("tmp" unless it's
                a final deliverable).
            save_relative_path: path relative to `save_directory`.

        Returns ToolResult.data: dict with 'downloaded' (bool), 'path'
        (absolute), 'suggested_filename', 'failure' (str or None).
        """
        try:
            save_path = self._resolve_workspace_path(save_directory, save_relative_path)
        except ValueError as error:
            return ToolResult.fail(error)

        granted = self.ask_permission(
            f"Agent is trying to download {file_url!r} to "
            f"{save_directory}/{save_relative_path}.",
            level=PERMISSION_LEVEL_WRITE,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to download the file.")
            )

        try:
            data = self._run_on_moodle_thread(
                lambda moodle: moodle.download_file(file_url, save_path)
            )
            return ToolResult.ok(data)
        except Exception as error:
            return ToolResult.fail(error)

    # --------------------------------------------------------------------------- #
    # Other
    # --------------------------------------------------------------------------- #

    @require_permission(level=PERMISSION_LEVEL_WRITE)
    def send_email(
        self, recipient: str, subject: str, content: str = ""
    ) -> ToolResult[None]:
        """Send an email to `recipient` with `subject`/`content`."""

        # TODO: brancher ici l'envoi réel (SMTP, API, ...).
        return ToolResult.ok()

    @require_permission(level=PERMISSION_LEVEL_READ)
    def get_location(self) -> ToolResult[Dict[str, Optional[str]]]:
        """Get device location. Returns dict: city/region/country/loc."""
        try:
            response = requests.get(LOCATION_URL, timeout=5)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            return ToolResult.fail(error)
        except ValueError as error:  # JSON invalide
            return ToolResult.fail(error)

        return ToolResult.ok(
            {
                "city": data.get("city"),
                "region": data.get("region"),
                "country": data.get("country"),
                "loc": data.get("loc"),
            }
        )

    @require_permission(level=PERMISSION_LEVEL_READ)
    def get_weather(
        self,
        latitude: float,
        longitude: float,
        resolution: Literal["current", "hourly", "daily"] = "current",
        variables: Optional[List[str]] = None,
        forecast_days: int = 7,
        timezone: str = "auto",
    ) -> ToolResult[Dict[str, Any]]:
        """Get weather from Open-Meteo for (latitude, longitude).

        Args:
            resolution: "current"/"hourly"/"daily".
            variables: variables list, None = defaults.
            forecast_days: 1-16 (ignored for "current").
            timezone: e.g. "Europe/Paris" or "auto".

        Returns compact dict (only requested block, floats rounded to 1dp).
        """
        if resolution not in DEFAULT_WEATHER_VARIABLES:
            return ToolResult.fail(
                ValueError(
                    f"Unknown resolution {resolution!r}, expected one of "
                    f"{', '.join(DEFAULT_WEATHER_VARIABLES)}."
                )
            )

        if not variables:
            variables = DEFAULT_WEATHER_VARIABLES[resolution]

        params: Dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            resolution: ",".join(variables),
        }
        if resolution != "current":
            params["forecast_days"] = max(1, min(forecast_days, MAX_FORECAST_DAYS))

        try:
            response = requests.get(WEATHER_URL, params=params, timeout=10)
            response.raise_for_status()
            return ToolResult.ok(self._compact_weather(response.json(), resolution))
        except requests.RequestException as error:
            return ToolResult.fail(error)
