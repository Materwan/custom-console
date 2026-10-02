"""The agent console: wires settings, tools, the agno agent and the terminal UI."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.text import Text

from ..fs import FileManager
from ..llm.errors import ProviderUnavailableError
from ..llm.keys import KeyStore
from ..llm.ollama import ModelInfo, OllamaClient
from ..llm.providers import Provider, ProviderMemory, get_provider
from ..settings import Settings
from .cache import JsonCache
from .checkpoints import Checkpoints
from .commands import AgentCommands
from .context import ContextManager, choose_window, estimate_messages, summarize_with
from .factory import build_agent, build_model, build_subagent, tools_token_estimate
from .journal import JsonlLogger
from .permissions import PermissionGate, permission_label
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
        model: str,
        name: str,
        permission_level: int,
        files: FileManager,
        memory: bool = True,
        console: Optional[Console] = None,
        agent_factory: Callable[..., Any] = build_agent,
        model_factory: Callable[..., Any] = build_model,
        subagent_factory: Callable[..., Any] = build_subagent,
        ollama: Optional[OllamaClient] = None,
        provider: str = "ollama",
        api_key: Optional[str] = None,
        keys: Optional[KeyStore] = None,
        catalogs: Optional[Callable[[Provider, Optional[str]], Any]] = None,
        **screen_options: Any,
    ) -> None:
        """`provider` serves `model` (see `llm.providers`); its `api_key` is looked up
        in `keys` when not given. `catalogs(provider, key)` lists a provider's models
        (default: the provider's own catalog; `ollama` for the local Ollama)."""
        self.settings = settings
        self.console = console or Console()
        self.files = files
        self.name = name
        self.model = model
        self.model_factory = model_factory
        self.ollama = ollama or OllamaClient(settings.ollama_host)
        self.keys = keys or KeyStore()
        self.catalogs = catalogs or (lambda p, key: self.ollama if p.local else p.catalog(settings, key))
        self.provider_memory = ProviderMemory(settings.agent_provider_path)
        self.provider = get_provider(provider)
        if api_key is None and self.provider.needs_key:
            api_key = self.keys.get(self.provider.key_variable)  # type: ignore[arg-type]
        self.api_key = api_key
        self.catalog = self.catalogs(self.provider, api_key)
        self.renderer = Renderer(self.console)
        settings.agent_dir.mkdir(parents=True, exist_ok=True)

        try:
            info: Optional[ModelInfo] = self.catalog.info(model)
        except ProviderUnavailableError:
            info = None
        window, self.num_ctx = choose_window(info, settings.agent_num_ctx)
        self.provider_memory.remember(self.provider.name, model)

        directory = files.local_location if files.mode == files.MODE_LOCAL else None
        zone = FreeZone.around(directory)
        self.checkpoints = Checkpoints(settings.agent_checkpoints_dir)
        # Sessions are saved per working directory; --no-memory saves nothing.
        self.store = SessionStore(settings.agent_sessions_dir, directory if memory else None, settings.agent_keep_sessions)
        self.record = self.store.new_record()
        self.context = ContextManager(
            base_session_id=self._base_id(self.record),
            window=window,
            project_file=zone.root / settings.agent_project_file if zone.root else None,
            compact_percent=settings.agent_compact_percent,
        )
        reads, todos = ReadTracker(), TodoList()
        self.session = AgentSession(
            JsonlLogger(settings.agent_log_path),
            settings.agent_user_id,
            self.context,
            UsageLedger(settings.agent_usage_path),
            model=model,
            checkpoints=self.checkpoints,
            reads=reads,
            todos=todos,
        )
        self.session.summarize = lambda transcript, previous, focus: summarize_with(self._chat, transcript, previous, focus)
        self.session.on_turn = self._turn_done

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
        self.subagents = SubAgents(
            self.session,
            tools=lambda: self.toolset.enabled(),
            build=lambda tools, hook, instructions: subagent_factory(
                settings, self.model, tools, hook, instructions, self.num_ctx, **self._model_options()
            ),
            location=lambda: files.location,
        )

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
        self.session.agent = agent_factory(
            settings, model, name, self.toolset.enabled(), self.session.tool_hook, memory, self.num_ctx, **self._model_options()
        )
        self._sync_tools(set_agent=False)
        self._observe_stored_history()

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
        record.provider = self.provider.name
        record.permission_level = int(self.tool_context.gate.auto_level)
        record.disabled_tools = self.toolset.disabled_names()
        record.todos = self.tool_context.todos.to_list()
        record.context = self.context.state()
        for dropped in self.store.save(record):
            self._forget_model_memory(dropped)

    def _forget_model_memory(self, record: SessionRecord) -> None:
        """Best effort: drop the agent database's copy of a session that is no longer kept."""
        database = getattr(self.session.agent, "db", None)
        for session_id in record.session_ids():
            try:
                database.delete_session(session_id)
            except Exception:
                pass

    def new_session(self) -> None:
        """``/clear``: a new conversation that is saved apart from the old one."""
        self.record = self.store.new_record()
        self.session.clear(self._base_id(self.record))
        self.record.context = self.context.state()

    def restore_session(self, record: SessionRecord) -> List[str]:
        """Go back to a saved session: the agent's conversation, provider, model, tools,
        permission level and checklist. Returns the warnings to show."""
        notes: List[str] = []
        if record.provider and record.provider != self.provider.name:
            notes.extend(self._restore_provider(record))
        elif record.model and record.model != self.model:
            try:
                self.switch_model(record.model)
            except (LookupError, ProviderUnavailableError) as error:
                notes.append(f"Model {record.model} is not available ({error}): keeping {self.model}.")
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
        self._observe_stored_history()
        try:
            remembered = self.session.agent.get_chat_history(session_id=self.context.session_id)
        except Exception:
            remembered = None
        if not remembered and not self.context.summary and record.turns:
            notes.append("The agent's own memory of this conversation is gone: you can read it above, but it will not remember it.")
        self.save_session()
        return notes

    def _observe_stored_history(self) -> None:
        """Count the conversation already stored for this session, so that the
        context meter is right before the first turn of a resumed conversation."""
        try:
            history = self.session.agent.get_chat_history(session_id=self.context.session_id) or []
            self.context.observe(None, estimate_messages(history))
        except Exception:
            pass  # only the meter is affected

    # -- tools --------------------------------------------------------------------- #

    def _sync_tools(self, set_agent: bool = True) -> None:
        """Give the agent the tools that are on, and account for them in the context."""
        enabled = self.toolset.enabled()
        if set_agent:
            self.session.agent.tools = enabled
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
        where = "" if self.provider.local else f" ({self.provider.name})"
        return f"{self.name} · {self.model}{where}"

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
        return Group(
            Text(self.name, style="bold cyan"),
            Text(
                f"model {self.model} · {self.provider.label} · permissions: {permission_label(permission_level)}",
                style="dim",
            ),
            Text(zone_line, style="dim"),
            *hint,
            Text("Type /help for the commands.", style="dim"),
            Text(""),
        )

    # -- model ----------------------------------------------------------------------- #

    def _model_options(self) -> dict:
        """What the agent and model factories need to know about the provider."""
        return {"provider": self.provider.name, "api_key": self.api_key}

    def _chat(self, messages: List[dict]) -> str:
        """One plain answer of the current model (the compaction summary)."""
        return self.provider.chat(self.settings, self.model, self.api_key, messages, self.num_ctx)

    def switch_model(self, name: str) -> ModelInfo:
        """Use another model of the current provider from the next turn on; the conversation is kept."""
        info = self.catalog.info(name)
        if info is None:
            where = "installed" if self.provider.local else f"available from {self.provider.label}"
            raise LookupError(f"{name} is not {where} (see /model for the list)")
        self._use(info)
        return info

    def _use(self, info: ModelInfo) -> None:
        window, self.num_ctx = choose_window(info, self.settings.agent_num_ctx)
        self.session.agent.model = self.model_factory(self.settings, info.name, self.num_ctx, **self._model_options())
        self.model = self.session.model = info.name
        self.context.window = window
        self.screen.title = self._title()
        self.provider_memory.remember(self.provider.name, info.name)

    # -- provider ------------------------------------------------------------------------- #

    def _history_has_tool_calls(self) -> bool:
        try:
            messages = self.session.agent.get_chat_history(session_id=self.context.session_id) or []
        except Exception:
            return False
        return any(getattr(m, "role", "") == "tool" or getattr(m, "tool_calls", None) for m in messages)

    def switch_provider(self, provider: Provider, api_key: Optional[str], catalog: Any, model: str) -> List[str]:
        """Use `model` of `provider` from the next turn on. Returns the notes to show.

        The conversation is kept. Tool calls made through Ollama cannot be replayed
        to OpenAI (they may lack the ids OpenAI requires), so a conversation that has
        some is first summarised, by the provider that made them, and continues from
        that summary.
        """
        info = catalog.info(model)
        if info is None:
            raise LookupError(f"{model} is not available from {provider.label}")
        notes: List[str] = []
        if provider.family != self.provider.family and provider.family == "openai" and self._history_has_tool_calls():
            try:
                before, after = self.session.compact()
            except Exception as error:
                raise ProviderUnavailableError(
                    f"the conversation could not be summarised to carry it over ({error}): use /clear to start afresh"
                ) from error
            notes.append(f"The conversation was summarised to carry it over ({before:.0f}% → {after:.0f}% of the context).")
        self.provider, self.api_key, self.catalog = provider, api_key, catalog
        self._use(info)
        self.save_session()
        return notes

    def _restore_provider(self, record: SessionRecord) -> List[str]:
        """Go back to the provider (and model) of a saved session, with its saved key."""
        try:
            provider = get_provider(record.provider)
            key = self.keys.get(provider.key_variable) if provider.needs_key else None  # type: ignore[arg-type]
            if provider.needs_key and not key:
                raise LookupError(f"no API key for it (/provider {provider.name} asks for one)")
            catalog = self.catalogs(provider, key)
            if catalog.info(record.model) is None:
                raise LookupError(f"{record.model} is not available there")
        except (LookupError, ProviderUnavailableError) as error:
            return [f"The session used {record.model} from {record.provider}, which is not available ({error}): keeping {self.model}."]
        # The session's own conversation came from that provider: nothing to carry over.
        self.provider, self.api_key, self.catalog = provider, key, catalog
        self._use(catalog.info(record.model))
        return []

    # -- lifecycle -------------------------------------------------------------------- #

    def run(self) -> None:
        try:
            self.screen.run()
        finally:
            self.tool_context.close()
