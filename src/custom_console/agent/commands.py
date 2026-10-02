"""The agent's slash commands: /model /usage /context /compact /clear /undo /init
/todo /permissions, plus the file commands of the shell (/ls /cd /cat ...)."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Iterable, Iterator, List, Optional

from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..llm.errors import ProviderUnavailableError
from ..llm.keys import FROM_ENVIRONMENT, SAVED
from ..llm.ollama import format_size
from ..llm.providers import PROVIDERS, connect, describe_model, find_model, get_provider, preferred_model
from ..shell.commands import build_file_registry
from ..shell.printer import QuietPrinter
from ..shell.repl import Shell
from .permissions import PermissionLevel, permission_label
from .render import turn_renderables
from .sessions import ago
from .slash import CommandResult, SlashCommand, SlashRegistry
from .turn import TurnView
from .questions import Choice
from .ui import MenuItem
from .usage import format_count, usage_tables

if TYPE_CHECKING:
    from .console import AgentConsole

BAR_WIDTH = 24
RESTORE_MAX_TURNS = 40  # exchanges redrawn by /restore (the agent remembers all of them anyway)
FILE_COMMAND_ALIASES = {"rmdoc2pdf": ("rmdoc",)}  # /rmdoc is the short form

INIT_PROMPT = (
    "Explore this project (folder structure, README, configuration, main source files) and create "
    "the file {file} at the root of the working directory. It is read by an AI assistant at the "
    "start of every conversation, so keep it short and factual: what the project is, how it is "
    "organised, how to run it and test it, the conventions to follow, and anything surprising. "
    "If {file} already exists, read it first and improve it instead of starting over."
)


def synopsis(parser: argparse.ArgumentParser) -> str:
    """Short argument synopsis of a command, e.g. ``[-a] [PATH ...]``."""
    flags: List[str] = []
    positionals: List[str] = []
    for action in parser._actions:  # argparse has no public API for this
        if isinstance(action, argparse._HelpAction):
            continue
        if action.option_strings:
            flags.append(action.option_strings[0])
            continue
        name = str(action.metavar or action.dest)
        if action.nargs == "?":
            positionals.append(f"[{name}]")
        elif action.nargs == "*":
            positionals.append(f"[{name} ...]")
        elif action.nargs == "+":
            positionals.append(f"{name} ...")
        else:
            positionals.append(name)
    parts = [f"[{flag}]" for flag in flags] + positionals
    return " ".join(parts)


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


class AgentCommands:
    def __init__(self, app: "AgentConsole") -> None:
        self.app = app
        self.renderer = app.renderer
        self._models_cache: Optional[tuple] = None

    # -- helpers ------------------------------------------------------------------ #

    def _error(self, message: str) -> CommandResult:
        return CommandResult(self.renderer.text(message, "red"))

    def _info(self, message: str) -> CommandResult:
        return CommandResult(self.renderer.text(message, "dim"))

    def _installed(self):
        """The current provider's models, cached briefly: completion asks on every keystroke."""
        import time

        provider = self.app.provider.name
        cache = self._models_cache
        if cache is None or cache[2] != provider or time.monotonic() - cache[0] > 10:
            self._models_cache = (time.monotonic(), self.app.catalog.installed(), provider)
        return self._models_cache[1]

    # -- registration --------------------------------------------------------------- #

    def register(self, registry: SlashRegistry) -> None:
        add = registry.add
        add(SlashCommand("model", "show or change the model", self.model, "[MODEL]", self.complete_model))
        add(
            SlashCommand(
                "provider",
                "show or change the LLM provider (ollama, ollama-cloud, chatgpt)",
                self.provider,
                "[NAME | forget NAME]",
                self.complete_provider,
            )
        )
        add(SlashCommand("usage", "tokens used: this session and in total", self.usage))
        add(SlashCommand("context", "how full the context window is", self.context))
        add(SlashCommand("compact", "summarise the conversation to free context", self.compact, "[FOCUS]"))
        add(SlashCommand("clear", "new conversation, clear the screen", self.clear))
        add(SlashCommand("restore", "bring back a previous session of this folder", self.restore, "[list|N]", self.complete_restore))
        add(SlashCommand("undo", "undo the file changes of the last turn", self.undo))
        add(SlashCommand("init", "create the project instructions file", self.init))
        add(SlashCommand("todo", "show the agent's checklist", self.todo))
        add(SlashCommand("permissions", "show or set the auto-accept level", self.permissions, "[0|1|2]", self.complete_permissions))
        add(SlashCommand("tools", "choose the tools the agent may use", self.tools, "[list|on|off|reset]", self.complete_tools))

        self._register_file_commands(registry)

    def _register_file_commands(self, registry: SlashRegistry) -> None:
        file_registry = build_file_registry()
        self.shell = Shell(
            self.app.settings,
            registry=file_registry,
            printer=QuietPrinter(),
            files=self.app.files,
            confirm=lambda question: self.app.screen.ask_permission(question, default=False),
        )
        for command in file_registry:
            registry.add(
                SlashCommand(
                    command.name,
                    command.summary,
                    lambda arguments, name=command.name: self.run_file_command(name, arguments),
                    synopsis(command.parser),
                    lambda arguments, name=command.name: self.complete_file_command(name, arguments),
                    group="Files",
                ),
                *FILE_COMMAND_ALIASES.get(command.name, ()),
            )

    # -- file commands ---------------------------------------------------------------- #

    def run_file_command(self, name: str, arguments: str) -> CommandResult:
        """Run a shell file command in the agent's working directory and return
        what it printed."""
        capture = self.renderer.capture()
        self.shell.printer.console = capture
        self.shell.execute(f"{name} {arguments}".strip())
        text = capture.file.getvalue()  # type: ignore[union-attr]
        if name == "cd" and not text:
            text = self.renderer.text(self.app.files.location, "yellow")
        return CommandResult(text)

    def complete_file_command(self, name: str, arguments: str) -> Iterator[Completion]:
        yield from self.shell.completer.get_completions(Document(f"{name} {arguments}"), None)

    # -- /model -------------------------------------------------------------------------- #

    def model(self, arguments: str) -> CommandResult:
        provider = self.app.provider
        if arguments:
            try:
                info = self.app.switch_model(arguments)
            except (LookupError, ProviderUnavailableError) as error:
                return self._error(str(error))
            self.app.save_session()
            if provider.local:
                where = "served by ollama.com" if info.remote else "local; it is loaded on first use"
            else:
                where = provider.label
            return CommandResult(
                self.renderer.text(f"Model: {info.name} ({where}) · context window {self.app.context.window:,} tokens", "green")
            )

        try:
            models = self._installed()
        except ProviderUnavailableError as error:
            return self._error(str(error))
        title = "Installed models" if provider.local else f"Models of {provider.label}"
        table = Table(title=title, title_justify="left")
        table.add_column("")
        table.add_column("Model", style="green", no_wrap=True)
        table.add_column("Size", justify="right")
        table.add_column("Max context", justify="right")
        table.add_column("Where")
        for info in models:
            table.add_row(
                "●" if info.name == self.app.model else "",
                info.name,
                "-" if info.remote else format_size(info.size),
                format_count(info.context_length) if info.context_length else "?",
                provider.where(info),
            )
        hint = Text(
            "Type /model NAME to switch (Tab completes). The conversation is kept. /provider changes the provider.",
            style="dim",
        )
        return CommandResult(self.renderer.render(table, hint))

    def complete_model(self, arguments: str) -> Iterator[Completion]:
        try:
            models = self._installed()
        except Exception:  # the provider is not reachable: nothing to suggest
            return
        for info in models:
            if info.name.lower().startswith(arguments.lower()) or arguments.lower() in info.name.lower():
                meta = describe_model(info) or self.app.provider.where(info)
                if info.name == self.app.model:
                    meta += " · current"
                yield Completion(info.name, start_position=-len(arguments), display_meta=meta)

    # -- /provider ------------------------------------------------------------------------- #

    PROVIDER_USAGE = "Usage: /provider [ollama | ollama-cloud | chatgpt | forget NAME]"

    def provider(self, arguments: str) -> CommandResult:
        words = arguments.split()
        if not words:
            return CommandResult(self.renderer.render(self._providers_table()))
        if words[0].lower() == "forget":
            return self._forget_key(words[1:])
        if len(words) != 1:
            return self._error(self.PROVIDER_USAGE)
        try:
            provider = get_provider(words[0])
        except LookupError as error:
            return self._error(str(error))

        app = self.app
        try:
            connection = connect(
                provider, app.keys, lambda prompt: app.screen.ask_text(prompt, secret=True), app.catalogs
            )
        except ProviderUnavailableError as error:
            return self._error(f"{provider.label}: {error}")
        if connection is None:
            return self._info("No key given: provider unchanged.")

        models = connection.models
        wanted = app.model if provider.name == app.provider.name else preferred_model(provider, app.settings, app.provider_memory)
        current = find_model(models, wanted)
        initial = models.index(current) if current is not None else 0
        answer = app.screen.ask_choice(
            f"Model to use with {provider.label}",
            [Choice(info.name, describe_model(info)) for info in models],
            allow_other=False,
            initial=initial,
        )
        text = "".join(self.renderer.text(note, "dim") for note in connection.notes)
        if answer is None:
            return CommandResult(text + self.renderer.text("Provider unchanged.", "dim"))
        try:
            notes = app.switch_provider(provider, connection.key, connection.catalog, answer.selected[0])
        except (LookupError, ProviderUnavailableError) as error:
            return CommandResult(text + self.renderer.text(f"Provider unchanged: {error}", "red"))
        for note in notes:
            text += self.renderer.text(note, "yellow")
        text += self.renderer.text(
            f"Provider: {provider.label} · model {app.model} · context window {app.context.window:,} tokens", "green"
        )
        return CommandResult(text)

    def _key_state(self, provider) -> str:
        if not provider.needs_key:
            return "not needed"
        source = self.app.keys.source(provider.key_variable)
        if source == FROM_ENVIRONMENT:
            return f"from {provider.key_variable}"
        if source == SAVED:
            return "saved"
        return "missing (asked on first use)"

    def _providers_table(self) -> Table:
        app = self.app
        table = Table(title="LLM providers", title_justify="left")
        table.add_column("")
        table.add_column("Name", style="green", no_wrap=True)
        table.add_column("Provider")
        table.add_column("API key")
        table.add_column("Last model")
        for provider in PROVIDERS.values():
            current = provider.name == app.provider.name
            table.add_row(
                "●" if current else "",
                provider.name,
                provider.label,
                self._key_state(provider),
                app.model if current else (app.provider_memory.last_model(provider.name) or "-"),
            )
        return table

    def _forget_key(self, words: List[str]) -> CommandResult:
        if len(words) != 1:
            return self._error(self.PROVIDER_USAGE)
        try:
            provider = get_provider(words[0])
        except LookupError as error:
            return self._error(str(error))
        if not provider.needs_key:
            return self._info(f"{provider.label} needs no key.")
        try:
            forgotten = self.app.keys.forget(provider.key_variable)
        except Exception as error:
            return self._error(f"Could not delete the key: {error}")
        message = f"Saved key of {provider.label} deleted." if forgotten else f"No saved key for {provider.label}."
        if self.app.keys.source(provider.key_variable) == FROM_ENVIRONMENT:
            message += f" {provider.key_variable} is still set in the environment (.env)."
        return self._info(message)

    def complete_provider(self, arguments: str) -> Iterator[Completion]:
        words = arguments.split(" ")
        last = words[-1].lower()
        if len(words) > 2 or (len(words) == 2 and words[0].lower() != "forget"):
            return
        choices = [(p.name, p.label) for p in PROVIDERS.values()]
        if len(words) == 1:
            choices.append(("forget", "delete a saved API key"))
        else:
            choices = [(p.name, p.label) for p in PROVIDERS.values() if p.needs_key]
        for name, meta in choices:
            if name.startswith(last):
                if name == self.app.provider.name:
                    meta += " · current"
                yield Completion(name, start_position=-len(last), display_meta=meta)

    # -- /usage, /context ------------------------------------------------------------------ #

    def usage(self, arguments: str) -> CommandResult:
        session = self.app.session
        tables = usage_tables(session.usage.summary(), session.usage.session_models)
        note = Text(
            "Totals of the turns run through this console (Ollama has no account-wide usage API).",
            style="dim",
        )
        return CommandResult(self.renderer.render(*tables, note))

    def context(self, arguments: str) -> CommandResult:
        report = self.app.context.breakdown()
        table = Table(
            title=f"Context · {self.app.model} · {report.used:,} / {report.window:,} tokens ({report.percent:.0f}%)",
            title_justify="left",
        )
        table.add_column("Part")
        table.add_column("Tokens", justify="right")
        table.add_column("Share", justify="right")
        table.add_column("")

        def row(label: str, tokens: int, style: str = "") -> None:
            share = tokens / report.window if report.window else 0
            table.add_row(label, f"{tokens:,}", f"{100 * share:.1f}%", Text(bar(share), style=style))

        row("System prompt", report.instructions)
        if report.project:
            row(f"Project file ({self.app.settings.agent_project_file})", report.project)
        if report.summary:
            row("Conversation summary", report.summary)
        row(f"Tools ({report.tool_count})", report.tools)
        row("Messages", report.messages)
        row("Free space", report.free, "dim")

        note = Text(
            "Parts are estimates (about 3.5 characters per token); the total is what the model reported "
            f"when it did. Compaction runs at {self.app.settings.agent_compact_percent}%, or with /compact.",
            style="dim",
        )
        return CommandResult(self.renderer.render(table, note))

    # -- /compact, /clear, /undo, /init, /todo, /permissions ------------------------------------- #

    def compact(self, arguments: str) -> CommandResult:
        try:
            before, after = self.app.session.compact(arguments)
        except LookupError as error:
            return self._info(str(error))
        except Exception as error:
            return self._error(f"Could not compact the conversation: {error}")
        self.app.save_session()
        summary = self.app.context.summary
        return CommandResult(
            self.renderer.text(f"Conversation compacted: {before:.0f}% → {after:.0f}% of the context.", "green")
            + self.renderer.text(summary, "dim")
        )

    def clear(self, arguments: str) -> CommandResult:
        self.app.new_session()
        return CommandResult(clear_screen=True)

    # -- /restore ----------------------------------------------------------------------------- #

    RESTORE_USAGE = "Usage: /restore [list | N]   (N: a number from /restore list; no argument: the latest previous session)"

    def restore(self, arguments: str) -> CommandResult:
        app = self.app
        if not app.store.available:
            return self._error("No saved sessions: they need a local working directory, and are not kept with --no-memory.")
        word = arguments.strip().lower()
        sessions = app.store.list()
        if word == "list":
            if not sessions:
                return self._info(f"No saved session for {app.store.directory}.")
            return CommandResult(self.renderer.render(self._sessions_table(sessions)))
        if not word:
            others = [s for s in sessions if s.id != app.record.id]
            if not others:
                return self._info(f"No previous session for {app.store.directory}.")
            target = others[0]
        elif word.isdigit() and 1 <= int(word) <= len(sessions):
            target = sessions[int(word) - 1]
            if target.id == app.record.id:
                return self._info("That is the session you are in.")
        else:
            return self._error(self.RESTORE_USAGE)

        notes = app.restore_session(target)
        shown = target.turns[-RESTORE_MAX_TURNS:]
        parts = [Rule(f"Session of {target.when:%Y-%m-%d %H:%M} · {len(target.turns)} exchange(s)", style="dim")]
        if len(shown) < len(target.turns):
            parts.append(Text(f"… {len(target.turns) - len(shown)} earlier exchange(s) not shown", style="dim"))
        for turn in shown:
            parts.extend(turn_renderables(TurnView.from_dict(turn)))
        toolset = app.toolset
        todos = app.tool_context.todos
        facts = [
            f"model {app.model} ({app.provider.name})",
            f"{len(toolset.enabled())} of {len(toolset.names())} tools on",
            f"auto-accept level {app.tool_context.gate.auto_level} ({permission_label(app.tool_context.gate.auto_level)})",
        ]
        if todos.items:
            facts.append(f"checklist {todos.summary()}")
        parts.append(Rule("Restored: " + " · ".join(facts), style="dim"))
        text = self.renderer.render(*parts)
        for note in notes:
            text += self.renderer.text(note, "yellow")
        return CommandResult(text)

    def _sessions_table(self, sessions) -> Table:
        table = Table(title=f"Sessions of {self.app.store.directory}", title_justify="left")
        table.add_column("#", justify="right")
        table.add_column("When")
        table.add_column("First question", overflow="ellipsis", no_wrap=True, max_width=50)
        table.add_column("Model", style="green")
        table.add_column("Exchanges", justify="right")
        table.add_column("")
        for number, record in enumerate(sessions, start=1):
            table.add_row(
                str(number),
                ago(record.when),
                record.first_prompt,
                record.model,
                str(len(record.turns)),
                "current" if record.id == self.app.record.id else "",
            )
        return table

    def complete_restore(self, arguments: str) -> Iterator[Completion]:
        if " " in arguments:
            return
        if "list".startswith(arguments.lower()):
            yield Completion("list", start_position=-len(arguments), display_meta="show the saved sessions")
        for number, record in enumerate(self.app.store.list(), start=1):
            if str(number).startswith(arguments):
                meta = f"{record.first_prompt} · {ago(record.when)}"
                yield Completion(str(number), start_position=-len(arguments), display_meta=meta)

    def undo(self, arguments: str) -> CommandResult:
        checkpoints = self.app.checkpoints
        try:
            report = checkpoints.undo()
        except LookupError:
            return self._info("Nothing to undo. (Changes made by run_command are not tracked.)")
        return CommandResult(self.renderer.text("\n".join(report), "green"))

    def init(self, arguments: str) -> CommandResult:
        zone = self.app.tool_context.zone
        if not zone.active:
            return self._error(f"No free zone ({zone.reason}): start the agent inside a project folder.")
        return CommandResult(prompt=INIT_PROMPT.format(file=self.app.settings.agent_project_file))

    def todo(self, arguments: str) -> CommandResult:
        todos = self.app.tool_context.todos
        if not todos.items:
            return self._info("No checklist.")
        return CommandResult(self.renderer.text(todos.render() + f"\n({todos.summary()})"))

    def permissions(self, arguments: str) -> CommandResult:
        gate = self.app.tool_context.gate
        if arguments:
            try:
                level = int(arguments)
                PermissionLevel(level)
            except ValueError:
                return self._error("Usage: /permissions [0|1|2]")
            gate.auto_level = level
            self.app.save_session()
        zone = self.app.tool_context.zone
        return CommandResult(
            self.renderer.text(
                f"Auto-accept level {gate.auto_level}: {permission_label(gate.auto_level)}\n"
                f"Free zone (no question asked for files there): {zone.describe()}"
            )
        )

    # -- /tools ----------------------------------------------------------------------------------- #

    TOOLS_USAGE = "Usage: /tools [list | on NAME... | off NAME... | reset]   (NAME: a tool, a group such as files, or a unique prefix)"

    def tools(self, arguments: str) -> CommandResult:
        toolset = self.app.toolset
        word, _, rest = arguments.partition(" ")
        word = word.lower()
        if not word:
            return self._tools_menu()
        if word == "list":
            return CommandResult(self.renderer.render(self._tools_table()))
        if word == "reset":
            toolset.reset()
            self.app.apply_tools()
            return CommandResult(self.renderer.text(f"All {len(toolset.names())} tools are on.", "green"))
        if word in ("on", "off"):
            names, unknown = toolset.resolve(rest.split())
            if unknown or not names:
                problem = f"Unknown tool: {', '.join(unknown)}. " if unknown else ""
                return self._error(problem + self.TOOLS_USAGE)
            before = set(toolset.disabled_names())
            toolset.set_enabled({name: word == "on" for name in names})
            self.app.apply_tools()
            return CommandResult(self._tools_summary(before))
        return self._error(self.TOOLS_USAGE)

    def _tools_menu(self) -> CommandResult:
        toolset = self.app.toolset
        items = [
            MenuItem(
                tool.__name__,
                tool.__name__,
                toolset.describe(tool),
                group=label,
                checked=toolset.is_enabled(tool.__name__),
            )
            for label, tools in toolset.groups
            for tool in tools
        ]
        if not items:
            return self._info("The agent has no tools.")
        choice = self.app.screen.ask_menu("Tools the agent may use", items)
        if choice is None:
            return self._info("Tools unchanged.")
        before = set(toolset.disabled_names())
        toolset.set_enabled(choice)
        self.app.apply_tools()
        return CommandResult(self._tools_summary(before))

    def _tools_summary(self, disabled_before: set) -> str:
        toolset = self.app.toolset
        disabled = toolset.disabled_names()
        now = set(disabled)
        lines = []
        if now - disabled_before:
            lines.append("Turned off: " + ", ".join(sorted(now - disabled_before)))
        if disabled_before - now:
            lines.append("Turned on: " + ", ".join(sorted(disabled_before - now)))
        if not lines:
            lines.append("Tools unchanged.")
        lines.append(f"{len(toolset.names()) - len(disabled)} of {len(toolset.names())} tools are on (from the next message).")
        if not len(toolset.names()) - len(disabled):
            lines.append("The agent can only answer in words now.")
        return self.renderer.text("\n".join(lines), "green")

    def _tools_table(self) -> Table:
        toolset = self.app.toolset
        table = Table(title="Tools", title_justify="left")
        table.add_column("Group", style="cyan")
        table.add_column("Tool", no_wrap=True)
        table.add_column("")
        table.add_column("What it does", overflow="fold")
        for label, tools in toolset.groups:
            for tool in tools:
                on = toolset.is_enabled(tool.__name__)
                table.add_row(label, tool.__name__, "on" if on else "OFF", toolset.describe(tool), style="" if on else "dim")
        return table

    def complete_tools(self, arguments: str) -> Iterator[Completion]:
        toolset = self.app.toolset
        words = arguments.split(" ")
        last = words[-1].lower()
        if len(words) == 1:
            for name, meta in (
                ("list", "show every tool and whether it is on"),
                ("on", "turn tools on"),
                ("off", "turn tools off"),
                ("reset", "turn every tool on"),
            ):
                if name.startswith(last):
                    yield Completion(name, start_position=-len(last), display_meta=meta)
            return
        if words[0].lower() not in ("on", "off"):
            return
        wanted = words[0].lower() == "on"  # `on` suggests what is off, and the other way round
        taken = {word.lower() for word in words[1:-1]}
        for label, tools in toolset.groups:
            group = label.lower()
            if group.startswith(last) and group not in taken:
                yield Completion(group, start_position=-len(last), display_meta=f"group · {len(tools)} tools")
            for tool in tools:
                name = tool.__name__
                if name.lower().startswith(last) and name.lower() not in taken and toolset.is_enabled(name) != wanted:
                    yield Completion(name, start_position=-len(last), display_meta=toolset.describe(tool)[:60])

    def complete_permissions(self, arguments: str) -> Iterable[Completion]:
        for level in PermissionLevel:
            if str(int(level)).startswith(arguments):
                yield Completion(str(int(level)), start_position=-len(arguments), display_meta=permission_label(level))
