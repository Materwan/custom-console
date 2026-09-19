"""Console interactive pour un agent Agno + Ollama.

Le rendu du flux de l'agent est accumulé dans un buffer markdown (`LiveBuffer`)
qui sert de source de vérité unique : tout ce qui est affiché, y compris les
demandes de permission, y est conservé puis réaffiché une fois la réponse
terminée.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import time
import requests

from typing import Any, Callable, Dict, Generic, List, Literal, Optional, TypeVar

from .moodle_agent import MoodleAgent
from .config import *

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from agno.agent import Agent
from agno.models.base import Model
from agno.models.ollama import Ollama
from agno.db.sqlite import SqliteDb

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def is_yes(answer: str) -> bool:
    """Retourne True si la réponse de l'utilisateur vaut « oui » (vide = oui)."""
    return answer.strip().lower() in YES_ANSWERS


def _load_cache() -> Dict[str, Any]:
    if not os.path.exists(AGENT_CACHE_PATH):
        return {}
    try:
        if not os.path.exists(AGENT_CACHE_PATH):
            with open(AGENT_CACHE_PATH, "w") as _:
                pass
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


class UserPermissionDenied(Exception):
    """Levée / retournée quand l'utilisateur refuse une action de l'agent."""


class JsonlLogger:
    """Journalise les échanges (prompts, réponses, appels d'outils) dans un
    fichier séparé, au format JSON Lines (une entrée JSON par ligne).

    Chaque entrée contient au minimum :
    - `timestamp` : horodatage ISO 8601 (UTC) de l'événement.
    - `type`      : "prompt", "answer", "tool_call" ou "error".

    Le fichier est ouvert en mode ajout ("a") et chaque écriture est suivie
    d'un `flush()`, pour ne rien perdre en cas d'interruption du programme.
    """

    def __init__(self, path: str = AGENT_LOG_PATH):
        self.path = path

    def _write(self, entry: Dict[str, Any]) -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
        try:
            with open(self.path, "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(entry, ensure_ascii=False, default=str))
                log_file.write("\n")
        except OSError:
            # Le logging ne doit jamais faire planter l'agent.
            pass

    def log_prompt(self, prompt_text: str) -> None:
        self._write({"type": "prompt", "prompt": prompt_text})

    def log_answer(self, answer_text: str) -> None:
        self._write({"type": "answer", "answer": answer_text})

    def log_tool_call(
        self,
        function_name: str,
        arguments: Dict[str, Any],
        result: Any,
        duration: float,
        error: Optional[str] = None,
    ) -> None:
        self._write(
            {
                "type": "tool_call",
                "tool": function_name,
                "arguments": arguments,
                "duration_seconds": round(duration, 4),
                "result": result,
                "error": error,
            }
        )


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


# --------------------------------------------------------------------------- #
# Buffer d'affichage
# --------------------------------------------------------------------------- #


class LiveBuffer:
    """Accumule du markdown et le rend au fil de l'eau via `rich.live.Live`.

    Le `Live` est transitoire : la zone affichée est effacée à l'arrêt, ce qui
    évite tout doublon lors des pauses (`pause()` / `resume()`) et permet de
    réafficher proprement le texte final une seule fois.
    """

    def __init__(self, console: Console, min_interval: float = RENDER_INTERVAL):
        self.console = console
        self.min_interval = min_interval
        self.text = ""
        self._live: Optional[Live] = None
        self._running = False
        self._last_render = 0.0

    # -- cycle de vie ------------------------------------------------------- #

    def __enter__(self) -> "LiveBuffer":
        self._live = Live(
            Markdown(""),
            console=self.console,
            auto_refresh=False,
            vertical_overflow="visible",
            transient=True,
        )
        self._live.start(refresh=True)
        self._running = True
        return self

    def __exit__(self, *exc_info: Any) -> Literal[False]:
        self.stop()
        return False

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
        self._live = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # -- rendu -------------------------------------------------------------- #

    def append(self, text: str, force: bool = False) -> None:
        """Ajoute du texte au buffer et rafraîchit l'affichage si besoin."""
        if not text:
            return
        self.text += text
        self.render(force=force)

    def render(self, force: bool = False) -> None:
        """Redessine le buffer, en limitant la fréquence de rafraîchissement."""
        if self._live is None or not self._running:
            return

        now = time.monotonic()
        if not force and now - self._last_render < self.min_interval:
            return

        self._live.update(Markdown(self.text), refresh=True)
        self._last_render = now

    def pause(self) -> None:
        """Libère le terminal (la zone Live est effacée) sans perdre le buffer."""
        if self._live is not None and self._running:
            self._live.stop()
            self._running = False

    def resume(self) -> None:
        """Réaffiche l'intégralité du buffer et reprend le rendu incrémental."""
        if self._live is not None and not self._running:
            self._live.start(refresh=True)
            self._running = True
            self.render(force=True)


# --------------------------------------------------------------------------- #
# Console de l'agent
# --------------------------------------------------------------------------- #


class AgentConsole:
    """Boucle de dialogue et outils exposés à l'agent."""

    def __init__(
        self,
        console: Console,
        location: str,
        agent_model_name: str,
        agent_name: str,
        *,
        agent_storage: Optional[str] = AGENT_DB_PATH,
        agent_instructions: Optional[List[str]] = DEFAULT_AGENT_INSTRUCTIONS,
        markdown: Optional[bool] = True,
        auto_permission_level: Optional[int] = PERMISSION_LEVEL_NONE,
        user_id: Optional[str] = AGENT_USER_ID,
        session_id: Optional[str] = AGENT_SESSION_ID,
    ):
        self.console = console
        self.console.clear()
        self.running = True
        self.location = location
        self.auto_permission_level = auto_permission_level
        self.user_id = user_id
        self.session_id = session_id
        self.history = InMemoryHistory()
        self.agent: Optional[Agent] = None
        self.live_buffer: Optional[LiveBuffer] = None
        self.logger = JsonlLogger()
        self._moodle_agent: Optional[MoodleAgent] = None
        self._moodle_executor: Optional[ThreadPoolExecutor] = None

        if not os.path.exists(agent_storage):
            agent_storage = None
            self.console.print(
                f"Agent is running without memory because {agent_storage} doesn't exist."
            )

        self.agent = Agent(
            model=Ollama(agent_model_name),
            name=agent_name,
            db=SqliteDb(db_file=agent_storage),
            add_history_to_context=True,
            num_history_runs=2,
            update_memory_on_run=True,
            tools=self.get_agent_tools(),
            instructions=agent_instructions,
            markdown=markdown,
            tool_hooks=[self.logger_hook],
        )

    def get_agent_tools(self) -> List[Callable[..., ToolResult]]:
        return [
            self.send_email,
            self.get_location,
            self.get_weather,
            self.read_file,
            self.list_files,
            self.workspace_file_op,
            self.moodle_get_page_content,
            self.moodle_click_element,
            self.moodle_input_text,
            self.moodle_list_courses,
            self.moodle_get_course_structure,
            self.moodle_get_announcements,
            self.moodle_get_grades,
            self.moodle_download_file,
        ]

    # -- Moodle --------------------------------------------------------------- #

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

    def _print_location(self) -> None:
        read = "r" if os.access(self.location, os.R_OK) else ""
        write = "w" if os.access(self.location, os.W_OK) else ""
        execute = "x" if os.access(self.location, os.X_OK) else ""
        self.console.print(f"[yellow]{self.location} [green]{read}{write}{execute}")

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

    # -- outils de l'agent -------------------------------------------------- #

    def send_email(
        self, recipient: str, subject: str, content: str = ""
    ) -> ToolResult[None]:
        """Send an email to `recipient` with `subject`/`content`."""
        granted = self.ask_permission(
            f"Agent is trying to send an email to {recipient}:\n"
            f"Subject: {subject}\n{content}",
            level=PERMISSION_LEVEL_WRITE,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to send the email.")
            )

        # TODO: brancher ici l'envoi réel (SMTP, API, ...).
        return ToolResult.ok()

    def get_location(self) -> ToolResult[Dict[str, Optional[str]]]:
        """Get device location. Returns dict: city/region/country/loc."""
        granted = self.ask_permission(
            "Agent is trying to get your location.", level=PERMISSION_LEVEL_READ
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to get the location.")
            )

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

        granted = self.ask_permission(
            f"Agent is trying to get the weather at ({latitude}, {longitude}).",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to get the weather.")
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

    def read_file(self, file_name: str, encoding: str = "utf-8") -> ToolResult[str]:
        """Read contents of `file_name` (name or path, encoding default utf-8).
        Returns ToolResult with the file text."""
        path = os.path.join(self.location, os.path.expanduser(file_name))

        granted = self.ask_permission(
            f"Agent is trying to read {path}.", level=PERMISSION_LEVEL_READ
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied(f"The user refused to give access to {file_name}.")
            )

        if os.path.isdir(path):
            return ToolResult.fail(IsADirectoryError(f"{file_name} is a directory."))

        if not os.path.isfile(path):
            return ToolResult.fail(FileNotFoundError(f"{file_name} is not a file."))

        try:
            with open(path, "r", encoding=encoding) as file:
                return ToolResult.ok(file.read())
        except OSError as error:
            return ToolResult.fail(error)
        except UnicodeDecodeError as error:
            return ToolResult.fail(error)

    def list_files(self) -> ToolResult[List[str]]:
        """List files and directories in the base directory.
        Returns ToolResult.data: list[str] of names."""
        granted = self.ask_permission(
            f"Agent is trying to list the files and directories of {self.location}.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to list files and directories.")
            )

        try:
            return ToolResult.ok(sorted(os.listdir(self.location)))
        except OSError as error:
            return ToolResult.fail(error)

    def _get_folder_structure(self, folder_path: str, depth: int) -> str:
        """Parcourt récursivement le dossier et donne l'arboresence du fichiers."""
        structure = []
        base_depth = folder_path.rstrip(os.sep).count(os.sep)

        for root, dirs, files in os.walk(folder_path):
            current_depth = root.count(os.sep) - base_depth
            if current_depth > depth:
                dirs[:] = []  # Ne descend pas plus loin que la profondeur max
                continue

            indent = "    " * current_depth
            structure.append(f"{indent}{os.path.basename(root)}/")

            sub_indent = "    " * (current_depth + 1)
            for f in sorted(files):
                structure.append(f"{sub_indent}{f}")

        return "\n".join(structure)

    def get_folder_structure(self, folder_path: str, depth: int = 2) -> ToolResult[str]:
        """Give an indented text tree of `folder_path`, up to `depth` subfolders."""

        granted = self.ask_permission(
            f"Agent is trying to list the structure of {folder_path}.",
            level=PERMISSION_LEVEL_READ,
        )
        if not granted:
            return ToolResult.fail(
                UserPermissionDenied("The user refused to list the folder structure.")
            )

        try:
            # Vérification si le chemin existe et est un dossier
            if not os.path.isdir(folder_path):
                return ToolResult.fail(
                    FileNotFoundError(f"{folder_path} is not a valid directory.")
                )

            return ToolResult.ok(self._get_folder_structure(folder_path))
        except Exception as error:
            return ToolResult.fail(error)

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
        for outside file, use `read_file`, `list_files` and `get_folder_structure` instead.

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

    # -- hooks -------------------------------------------------------------- #

    def logger_hook(
        self,
        function_name: str,
        function_call: Callable[..., ToolResult],
        arguments: Dict[str, Any],
    ) -> ToolResult:
        """Trace l'appel d'un outil dans le buffer d'affichage."""
        self.live_print(
            f"\n\n*Calling `{function_name}` with arguments:* `{arguments}`\n\n"
        )

        start_time = time.monotonic()
        try:
            result = function_call(**arguments)
        except Exception as error:  # l'outil ne doit jamais casser la boucle
            duration = time.monotonic() - start_time
            self.live_print(
                f"\n\n*`{function_name}` raised after {duration:.2f}s:* " f"{error}\n\n"
            )
            self.logger.log_tool_call(
                function_name, arguments, None, duration, error=str(error)
            )
            return ToolResult.fail(error)

        duration = time.monotonic() - start_time

        if not isinstance(result, ToolResult):
            self.live_print(f"\n\n*`{function_name}` ran in {duration:.2f}s.*\n\n")
            self.logger.log_tool_call(function_name, arguments, result, duration)
            return result

        if result.success:
            message = f"\n\n*`{function_name}` succeeded in {duration:.2f}s.*\n\n"
        else:
            message = (
                f"\n\n*`{function_name}` failed in {duration:.2f}s:* "
                f"{result.error}\n\n"
            )
        self.live_print(message)
        self.logger.log_tool_call(
            function_name,
            arguments,
            result.to_dict(),
            duration,
        )

        return result

    # -- boucle principale -------------------------------------------------- #

    def process_command(self, instruction: str) -> bool:
        """Traite les commandes internes. Retourne True si elle a été gérée."""
        command = instruction.split(" ", 1)[0]

        if command == "/bye":
            self.running = False
            return True

        if command == "/clear":
            self.console.clear()
            return True

        if command.startswith("/"):
            self.console.print(f"[red]Unknown command: {command}")
            return True

        return False

    def run(self) -> None:
        if self.agent is None:
            raise RuntimeError("No agent set, call set_agent() first.")

        while self.running:
            self._print_location()

            try:
                instruction = prompt(f"[{self.agent.name}]> ", history=self.history)
            except KeyboardInterrupt:
                self.console.line()
                continue
            except EOFError:
                self.console.line()
                break

            instruction = instruction.strip()
            if not instruction or self.process_command(instruction):
                continue

            self.logger.log_prompt(instruction)
            answer = self._stream_answer(instruction)
            self.logger.log_answer(answer)
            self._print_exchange(instruction, answer)

    def _stream_answer(self, instruction: str) -> str:
        """Diffuse la réponse de l'agent et retourne le texte complet."""
        assert self.agent is not None

        self.console.line()

        with LiveBuffer(self.console) as live:
            self.live_buffer = live
            try:
                for chunk in self.agent.run(
                    instruction,
                    stream=True,
                    user_id=self.user_id,
                    session_id=self.session_id,
                ):
                    content = getattr(chunk, "content", None)
                    if isinstance(content, str) and content:
                        live.append(content)
            except KeyboardInterrupt:
                live.append("\n\n*Interrupted by the user.*\n")
            except Exception as error:
                live.append(f"\n\n*Agent error:* {error}\n")
            finally:
                live.render(force=True)
                answer = live.text
                self.live_buffer = None

        return answer

    def _print_exchange(self, instruction: str, answer: str) -> None:
        """Réaffiche l'échange une fois le rendu Live terminé (et effacé)."""
        assert self.agent is not None

        self._print_location()
        self.console.print(f"[{self.agent.name}]> {instruction}", markup=False)
        self.console.line()
        if answer.strip():
            self.console.print(Markdown(answer))
        self.console.line()


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #


def main() -> None:

    auto_permission_level = int(AUTO_AGENT_PREMISSION)

    agent_console = AgentConsole(
        Console(),
        os.getcwd(),
        "gemma4:31b-cloud",
        "My Agent",
        auto_permission_level=auto_permission_level,
    )
    agent_console.run()


if __name__ == "__main__":
    main()
