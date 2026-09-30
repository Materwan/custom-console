"""Console interactive pour un agent Agno + Ollama.

Le rendu du flux de l'agent est accumulé dans un buffer markdown (`LiveBuffer`)
qui sert de source de vérité unique : tout ce qui est affiché, y compris les
demandes de permission, y est conservé puis réaffiché une fois la réponse
terminée.
"""

from __future__ import annotations

import json
import os
import time

from typing import Any, Callable, Dict, List, Literal, Optional

from .moodle_agent import MoodleAgent
from custom_console.config import *
from custom_console.utils.file_utils import *
from .tools import *

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.models.ollama import Ollama
from agno.db.sqlite import SqliteDb


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
        agent_model_name: str,
        agent_name: str,
        *,
        agent_storage: Optional[Path] = AGENT_DB_PATH,
        agent_instructions: Optional[List[str]] = AGENT_INSTRUCTIONS,
        markdown: Optional[bool] = True,
        auto_permission_level: Optional[int] = PERMISSION_LEVEL_NONE,
        user_id: Optional[str] = AGENT_USER_ID,
        session_id: Optional[str] = AGENT_SESSION_ID,
    ):
        self.console = console
        self.console.clear()
        self.file_manager = FileManager()
        self.running = True
        self.user_id = user_id
        self.session_id = session_id
        self.history = InMemoryHistory()
        self.agent: Optional[Agent] = None
        self.live_buffer: Optional[LiveBuffer] = None
        self.logger = JsonlLogger()

        if not os.path.exists(agent_storage):
            agent_storage = None
            self.console.print(
                f"Agent is running without memory because {agent_storage} doesn't exist."
            )
            agent_db = None
            add_history_to_context = False
            update_memory_on_run = False

        else:
            agent_storage = str(agent_storage).replace("\\", "/")
            agent_db = SqliteDb(agent_storage)
            add_history_to_context = True
            update_memory_on_run = True

        self.agent_tools = AgentTool(
            self.file_manager, self.console, self.live_buffer, auto_permission_level
        )

        self.agent = Agent(
            model=Ollama(agent_model_name),
            name=agent_name,
            db=agent_db,
            add_history_to_context=add_history_to_context,
            num_history_runs=2,
            update_memory_on_run=update_memory_on_run,
            tools=self.agent_tools.get_agent_tools(),
            instructions=agent_instructions,
            use_instruction_tags=True,
            markdown=markdown,
            tool_hooks=[self.logger_hook],
        )

    # --------------------------------------------------------------------------- #
    # Helper
    # --------------------------------------------------------------------------- #

    def _print_location(self) -> None:
        read = "r" if os.access(self.file_manager.location, os.R_OK) else ""
        write = "w" if os.access(self.file_manager.location, os.W_OK) else ""
        execute = "x" if os.access(self.file_manager.location, os.X_OK) else ""
        self.console.print(
            f"[yellow]{self.file_manager.location} [green]{read}{write}{execute}"
        )

    def live_print(self, text: str) -> None:
        """Écrit dans le buffer si un rendu Live est en cours, sinon direct."""
        if self.live_buffer is not None:
            self.live_buffer.append(text, force=True)
        else:
            self.console.print(text)

    def _stream_answer(self, instruction: str) -> Tuple[str, RunOutput]:
        assert self.agent is not None
        self.console.line()

        start_time = time.perf_counter()

        with LiveBuffer(self.console) as live:
            self.live_buffer = live
            run_output: Optional[RunOutput] = None
            try:
                for chunk in self.agent.run(
                    instruction,
                    stream=True,
                    yield_run_output=True,
                    user_id=self.user_id,
                    session_id=self.session_id,
                ):
                    if isinstance(chunk, RunOutput):
                        run_output = chunk
                        continue
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

        generation_time = time.perf_counter() - start_time

        return (answer, run_output, generation_time)

    def _print_exchange(
        self,
        instruction: str,
        answer: str,
        run_output: RunOutput,
        generation_time: float,
    ) -> None:
        """Réaffiche l'échange une fois le rendu Live terminé (et effacé)."""
        assert self.agent is not None

        self._print_location()
        self.console.print(f"[{self.agent.name}]> {instruction}", markup=False)
        self.console.line()
        if answer.strip():
            self.console.print(Markdown(answer))
        self.console.line()
        if run_output and run_output.metrics:
            m = run_output.metrics

            output_tokens = m.output_tokens or 0
            tokens_per_second = (
                output_tokens / generation_time if generation_time > 0 else 0
            )

            self.console.print(
                f"[italic][dim]"
                f"\nTokens : {m.input_tokens} prompt / "
                f"{output_tokens} completion "
                f"({m.total_tokens} total)"
                f"\nDurée : {generation_time:.2f} s"
                f"\nVitesse : {tokens_per_second:.2f} tokens/s"
                f"\n[/dim][/italic]",
                highlight=False,
            )

    # --------------------------------------------------------------------------- #
    # Hook
    # --------------------------------------------------------------------------- #

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

    # --------------------------------------------------------------------------- #
    # Main loop
    # --------------------------------------------------------------------------- #

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
            answer, run_output, generation_time = self._stream_answer(instruction)
            self.logger.log_answer(answer)
            self._print_exchange(instruction, answer, run_output, generation_time)


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #


def main() -> None:

    auto_permission_level = int(AUTO_AGENT_PREMISSION)

    agent_console = AgentConsole(
        Console(),
        "gemma4:31b-cloud",
        "My Agent",
        auto_permission_level=auto_permission_level,
    )
    agent_console.run()


if __name__ == "__main__":
    main()
