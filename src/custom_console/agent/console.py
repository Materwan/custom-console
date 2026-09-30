"""The agent console: wires settings, tools, the agno agent and the terminal UI."""

from __future__ import annotations

from typing import Any, Callable, Optional

from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.text import Text

from ..fs import FileManager
from ..settings import Settings
from .cache import JsonCache
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
        console: Optional[Console] = None,
        agent_factory: Callable[..., Any] = build_agent,
        **screen_options: Any,
    ) -> None:
        self.settings = settings
        self.console = console or Console()
        settings.agent_dir.mkdir(parents=True, exist_ok=True)

        self.session = AgentSession(
            JsonlLogger(settings.agent_log_path), settings.agent_user_id, settings.agent_session_id
        )
        self.screen = AgentScreen(
            title=f"{name} · {model}",
            console=self.console,
            turn_runner=self.session.run_turn,
            banner=Group(
                Text(f"{name}", style="bold cyan"),
                Text(f"model {model} · permissions: {permission_label(permission_level)}", style="dim"),
                Text("Type /help for the commands.", style="dim"),
                Text(""),
            ),
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
        tools = build_tools(self.tool_context)
        self.session.agent = agent_factory(
            settings, model, name, tools, self.session.tool_hook, memory
        )

    def run(self) -> None:
        try:
            self.screen.run()
        finally:
            self.tool_context.close()
