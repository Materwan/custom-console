"""The agent's slash commands: /model /provider /usage /context /compact /clear /undo /init
/todo /permissions, plus the file commands of the shell (/ls /cd /cat ...). /model and /provider
are the Clara server's own commands, run there."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Iterable, Iterator, List, Optional

from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .clara import ClaraError
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
        self._remote_commands: Optional[list] = None

    # -- helpers ------------------------------------------------------------------ #

    def _error(self, message: str) -> CommandResult:
        return CommandResult(self.renderer.text(message, "red"))

    def _info(self, message: str) -> CommandResult:
        return CommandResult(self.renderer.text(message, "dim"))

    # -- registration --------------------------------------------------------------- #

    def register(self, registry: SlashRegistry) -> None:
        add = registry.add
        add(SlashCommand("model", "show or change the server's model (needs CLARA_ADMIN_TOKEN)", self._on_server("model"), "[MODEL]", self._complete_on_server("model")))
        add(
            SlashCommand(
                "provider",
                "show or change where the server runs the model (needs CLARA_ADMIN_TOKEN)",
                self._on_server("provider"),
                "[local|cloud]",
                self._complete_on_server("provider"),
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

    # -- /model, /provider: the server's own commands ------------------------------------ #

    def _server_commands(self) -> list:
        """The server's console commands (for completion), asked once and kept."""
        if self._remote_commands is None:
            try:
                self._remote_commands = self.app.client.admin_commands()
            except ClaraError:
                self._remote_commands = []
        return self._remote_commands

    def _on_server(self, name: str):
        """A handler that runs `/name arguments` in the Clara server's console."""

        def handler(arguments: str) -> CommandResult:
            try:
                output = self.app.client.admin(f"/{name} {arguments}".strip())
                health = self.app.client.health() if arguments else None
            except ClaraError as error:
                return self._error(str(error))
            if health:  # the model or the provider may have changed
                self.app._model_changed(str(health.get("model") or self.app.model), str(health.get("provider") or ""))
            return CommandResult(self.renderer.text(output))

        return handler

    def _complete_on_server(self, name: str):
        def completer(arguments: str) -> Iterator[Completion]:
            if " " in arguments:
                return
            for entry in self._server_commands():
                if entry.get("name") == name:
                    for choice in entry.get("choices", []):
                        if choice.startswith(arguments.lower()):
                            yield Completion(choice, start_position=-len(arguments))

        return completer

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
        self.app.session.refresh_context()  # the server knows the real size
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
        row("Messages and Clara's memory", report.messages)
        row("Free space", report.free, "dim")

        note = Text(
            "Parts are estimates (about 3.5 characters per token); the total is what the model reported "
            "when it did. The server compacts the conversation when the context is nearly full, or on /compact.",
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
            f"model {app.model} ({app.provider_name})",
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
