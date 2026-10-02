"""The agent console: wires settings, tools, the Clara server and the terminal UI.

The model, the memory and the conversations live on the Clara server; this console is its
terminal client. The tools (files, shell, PDF, Moodle, mail...) stay here: the server asks for
them and the console runs them on this computer, after the permission questions.
"""

from __future__ import annotations

from typing import Any, List, Optional

from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.text import Text

from ..fs import FileManager
from ..settings import Settings
from .cache import JsonCache
from .checkpoints import Checkpoints
from .clara import ClaraClient, ClaraError
from .commands import AgentCommands
from .context import ContextManager
from .journal import JsonlLogger
from .permissions import PermissionGate, permission_label
from .remote import RemoteAgent
from .schema import tools_token_estimate
from .sessions import SessionRecord, SessionStore, ago
from .session import AgentSession
from .slash import CommandResult, Renderer, SlashCommand, SlashRegistry
from .subagent import SubAgents
from .toolset import ToolSet
from .tools import ToolContext, build_tool_groups
from .tools.state import ReadTracker, TodoList
from .turn import TurnView
from .ui import AgentScreen, add_basic_commands
from .usage import UsageLedger, format_count
from .zone import FreeZone

__all__ = ["AgentConsole", "permission_label"]


class AgentConsole:
    def __init__(
        self,
        *,
        settings: Settings,
        name: str,
        permission_level: int,
        files: FileManager,
        memory: bool = True,
        console: Optional[Console] = None,
        client: Optional[ClaraClient] = None,
        **screen_options: Any,
    ) -> None:
        """`client` is the connection to the Clara server (default: built from the settings).
        Raises :class:`ClaraError` when the server cannot be reached. With `memory` False the
        conversation is not saved here and is erased from the server when the console closes."""
        self.settings = settings
        self.console = console or Console()
        self.files = files
        self.name = name
        self.memory = memory
        self.client = client or ClaraClient(
            settings.clara_url,
            settings.clara_token,
            user_id=settings.agent_user_id,
            user_name=settings.clara_user_name,
            admin_token=settings.clara_admin_token,
        )
        self.renderer = Renderer(self.console)
        settings.agent_dir.mkdir(parents=True, exist_ok=True)

        health = self.client.health()
        self.model = str(health.get("model") or "?")
        self.provider_name = str(health.get("provider") or "")

        directory = files.local_location if files.mode == files.MODE_LOCAL else None
        zone = FreeZone.around(directory)
        self.checkpoints = Checkpoints(settings.agent_checkpoints_dir)
        # Sessions are saved per working directory; --no-memory saves nothing.
        self.store = SessionStore(settings.agent_sessions_dir, directory if memory else None, settings.agent_keep_sessions)
        self.record = self.store.new_record()
        self.conversations = [self._base_id(self.record)]  # every server conversation this run used
        self.context = ContextManager(
            base_session_id=self.conversations[0],
            project_file=zone.root / settings.agent_project_file if zone.root else None,
        )
        reads, todos = ReadTracker(), TodoList()
        self.session = AgentSession(
            JsonlLogger(settings.agent_log_path),
            settings.agent_user_id,
            self.context,
            UsageLedger(settings.agent_usage_path),
            model=self.model,
            checkpoints=self.checkpoints,
            reads=reads,
            todos=todos,
        )
        self.session.provider = self.provider_name
        self.session.on_turn = self._turn_done
        self.session.on_model = self._model_changed

        registry = SlashRegistry(self.renderer)
        add_basic_commands(registry)
        registry.add(SlashCommand("details", "show or hide what tool lines hide (or Ctrl+O)", self._toggle_details))
        self.screen = AgentScreen(
            title=self._title(),
            console=self.console,
            turn_runner=self.session.run_turn,
            banner=self._banner(permission_level, zone),
            history=FileHistory(str(settings.agent_history_path)),
            commands=registry,
            status=self._status,
            todos=todos.progress,
            transcript=lambda: [TurnView.from_dict(turn) for turn in self.record.turns],
            **screen_options,
        )
        self.subagents = SubAgents(self.session, tools=lambda: self.toolset.enabled(), location=lambda: files.location)

        self.tool_context = ToolContext(
            settings=settings,
            files=files,
            gate=PermissionGate(
                auto_level=permission_level,
                ask=self.screen.ask_permission,
                record=self.session.record_permission,
            ),
            cache=JsonCache(settings.agent_cache_path),
            zone=zone,
            checkpoints=self.checkpoints,
            reads=reads,
            todos=todos,
            is_cancelled=lambda: self.session.cancelled,
            ask_user=self.screen.ask_choice,
            run_subagent=self.subagents.run,
        )
        self.toolset = ToolSet(build_tool_groups(self.tool_context), settings.agent_tools_path)
        self.session.remote = RemoteAgent(self.client, self.toolset.enabled, settings.load_instructions)
        self._sync_tools()
        self.session.refresh_context()

        AgentCommands(self).register(registry)

    # -- saved sessions (/restore) ---------------------------------------------------- #

    def _base_id(self, record: SessionRecord) -> str:
        return f"{self.settings.agent_session_id}-{record.id}"

    def _turn_done(self, view: Any) -> None:
        self.record.turns.append(view.to_dict())
        self.save_session()

    def save_session(self) -> None:
        """Write the current session (once it has a turn) with the state it is in now."""
        if not self.store.available or not self.record.turns:
            return
        record = self.record
        record.model = self.model
        record.provider = self.provider_name
        record.permission_level = int(self.tool_context.gate.auto_level)
        record.disabled_tools = self.toolset.disabled_names()
        record.todos = self.tool_context.todos.to_list()
        record.context = self.context.state()
        for dropped in self.store.save(record):
            self._forget_conversation(dropped.conversation_id())

    def _forget_conversation(self, conversation: str) -> None:
        """Best effort: erase a conversation from the server (a session that is no longer kept)."""
        if not conversation:
            return
        try:
            self.client.forget(conversation)
        except ClaraError:
            pass

    def new_session(self) -> None:
        """``/clear``: a new conversation that is saved apart from the old one."""
        self.record = self.store.new_record()
        conversation = self._base_id(self.record)
        self.conversations.append(conversation)
        self.session.clear(conversation)
        self.record.context = self.context.state()

    def restore_session(self, record: SessionRecord) -> List[str]:
        """Go back to a saved session: the server conversation, tools, permission level and
        checklist. Returns the warnings to show."""
        notes: List[str] = []
        # Everything is set before anything is saved: saving writes the current state into the record.
        self.context.restore(record.context)
        self.tool_context.gate.auto_level = record.permission_level
        try:
            self.tool_context.todos.replace(record.todos)
        except ValueError:
            self.tool_context.todos.clear()
        self.tool_context.reads.clear()
        self.toolset.replace_disabled(record.disabled_tools)
        self._sync_tools()
        self.record = record
        if self.context.session_id not in self.conversations:
            self.conversations.append(self.context.session_id)
        info = self.session.refresh_context()
        if info is None:
            notes.append("The server could not be asked whether it remembers this conversation.")
        elif not info.get("messages") and not info.get("summary") and record.turns:
            notes.append("The server has no memory of this conversation any more: you can read it above, but Clara will not remember it.")
        self.save_session()
        return notes

    # -- tools --------------------------------------------------------------------- #

    def _sync_tools(self) -> None:
        """Account for the tools that are on in the context estimate."""
        enabled = self.toolset.enabled()
        self.context.disabled_tools = self.toolset.disabled_names()
        try:
            tokens = tools_token_estimate(enabled)
        except Exception:
            tokens = 0
        self.context.set_static(self.settings.load_instructions(), tokens, len(enabled))

    def apply_tools(self) -> None:
        """Use the tool selection of ``/tools`` from the next turn on."""
        self._sync_tools()
        self.save_session()

    # -- display ------------------------------------------------------------------- #

    def _title(self) -> str:
        where = f" ({self.provider_name})" if self.provider_name else ""
        return f"{self.name} · {self.model}{where}"

    def _model_changed(self, model: str, provider: str) -> None:
        """The server switched model or provider (its /provider and /model commands)."""
        self.model, self.provider_name = model, provider
        self.screen.title = self._title()

    def _toggle_details(self, arguments: str) -> CommandResult:
        shown = self.screen.toggle_details()
        state = "shown under their line" if shown else "hidden (one line per tool)"
        return CommandResult(self.renderer.text(f"Tool details are {state}. /transcript unfolds any of them.", "dim"))

    def _status(self) -> str:
        report = self.context.breakdown()
        return f"ctx {report.percent:.0f}% of {format_count(report.window)}"

    def _banner(self, permission_level: int, zone: FreeZone) -> Group:
        previous = self.store.list()
        hint = []
        if previous:
            last = previous[0]
            more = f" (+{len(previous) - 1} older: /restore list)" if len(previous) > 1 else ""
            hint = [
                Text(
                    f"Previous session: “{last.first_prompt}” · {ago(last.when)} · {len(last.turns)} exchange(s)"
                    f" — /restore{more}",
                    style="dim",
                )
            ]
        if zone.active:
            zone_line = f"free zone: {zone.root} (no questions asked for files there)"
        else:
            zone_line = f"free zone: none ({zone.reason}); every change asks"
        served = f"{self.model} on Clara ({self.provider_name or 'unknown provider'}) at {self.client.url}"
        return Group(
            Text(self.name, style="bold cyan"),
            Text(f"model {served} · permissions: {permission_label(permission_level)}", style="dim"),
            Text(zone_line, style="dim"),
            *hint,
            Text("Type /help for the commands.", style="dim"),
            Text(""),
        )

    # -- lifecycle -------------------------------------------------------------------- #

    def run(self) -> None:
        try:
            self.screen.run()
        finally:
            self.tool_context.close()
            if not self.memory:  # nothing kept: not here, and not on the server either
                for conversation in self.conversations:
                    self._forget_conversation(conversation)
