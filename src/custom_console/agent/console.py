"""The agent console: wires settings, tools, the agno agent and the terminal UI."""

from __future__ import annotations

from typing import Any, Callable, Optional

from prompt_toolkit.history import FileHistory
from rich.console import Group
from rich.text import Text

from ..fs import FileManager
from ..llm.ollama import OllamaClient
from ..settings import Settings
from .cache import JsonCache
from .commands import AgentCommands
from .factory import build_agent
from .journal import JsonlLogger
from .permissions import PermissionGate, PermissionLevel
from .session import AgentSession
from .tools import ToolContext, build_tools
from .ui import AgentScreen
from .workspace import Workspace

LEVEL_LABELS = {
    PermissionLevel.NONE: "always ask",
    PermissionLevel.READ: "reads are auto-accepted",
    PermissionLevel.WRITE: "everything is auto-accepted",
}


def permission_label(level: int) -> str:
    try:
        return LEVEL_LABELS[PermissionLevel(level)]
    except ValueError:
        return f"level {level}"


class AgentConsole:
    def __init__(
        self,
        *,
        settings: Settings,
        model: str,
        name: str,
        permission_level: int,
        files: FileManager,
        memory: bool = True,
        ollama: Optional[OllamaClient] = None,
        agent_factory: Callable[..., Any] = build_agent,
        **screen_options: Any,
    ) -> None:
        self.settings = settings
        self.model, self.name, self.memory = model, name, memory
        self.ollama = ollama or OllamaClient(settings.ollama_host)
        self._agent_factory = agent_factory
        settings.agent_dir.mkdir(parents=True, exist_ok=True)

        self.session = AgentSession(
            JsonlLogger(settings.agent_log_path), settings.agent_user_id, settings.agent_session_id
        )
        self.commands = AgentCommands(self)
        self.screen = AgentScreen(
            title=f"{name} · {model}",
            commands=self.commands.all(),
            turn_runner=self.session.run_turn,
            banner=self,
            history=FileHistory(str(settings.agent_history_path)),
            **screen_options,
        )

        self.tool_context = ToolContext(
            settings=settings,
            files=files,
            gate=PermissionGate(
                auto_level=permission_level,
                ask=self.screen.ask_permission,
                record=self.session.record_permission,
            ),
            workspace=Workspace(settings.workspace_roots),
            cache=JsonCache(settings.agent_cache_path),
        )
        self.tools = build_tools(self.tool_context)
        self.session.agent = self._build_agent()

    def __rich__(self) -> Group:
        """The banner at the top of the conversation."""
        level = self.tool_context.gate.auto_level
        return Group(
            Text(self.name, style="bold cyan"),
            Text(f"model {self.model} · permissions: {permission_label(level)}", style="dim"),
            Text("Type /help for the commands, /model to change the model.", style="dim"),
            Text(""),
        )

    def _build_agent(self) -> Any:
        return self._agent_factory(
            self.settings, self.model, self.name, self.tools, self.session.tool_hook, self.memory
        )

    # -- used by the slash commands (worker thread) --------------------------- #

    def switch_model(self, model: str) -> None:
        """Answer with another model from now on; the conversation continues."""
        self.model = model
        self.session.agent = self._build_agent()
        self.screen.title = f"{self.name} · {model}"

    def new_conversation(self) -> None:
        self.session.new_conversation()
        self.tool_context.memo.clear()
        self.screen.request_clear()

    def run(self) -> None:
        try:
            self.screen.run()
        finally:
            self.tool_context.close()
