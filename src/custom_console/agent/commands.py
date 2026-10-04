"""The agent's slash commands: /model /provider /remind /reminders /unremind /notify-after /usage /context /compact
/clear /undo /init /todo /permissions, plus the file commands of the shell (/ls /cd /cat ...). /model and /provider
are the Clara server's own commands, run there."""

from __future__ import annotations

import argparse
from datetime import datetime
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
from .reminders import REPEATS, local_time, parse_remind, take_targets
from .render import turn_renderables
from .sessions import ago
from .slash import CommandResult, SlashCommand, SlashRegistry
from .turn import TurnView
from .overlays import MenuItem
from .usage import usage_tables

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
        add(SlashCommand("remind", "a notification for you at a time, on your Clara clients", self.remind, "[daily|weekly|monthly] [@SURFACES] WHEN TEXT", self.complete_remind))
        add(SlashCommand("reminders", "your reminders that have not fired yet", self.reminders))
        add(SlashCommand("unremind", "cancel one of your reminders", self.unremind, "ID", self.complete_unremind))
        add(SlashCommand("notify-after", "how long a task takes before you are notified when it is done", self.notify_after, "[SECONDS|off|default]", self.complete_notify_after))
        add(SlashCommand("usage", "tokens used: this session and in total", self.usage))
        add(SlashCommand("context", "how full the context window is", self.context))
        add(SlashCommand("compact", "summarise the conversation to free context", self.compact, "[FOCUS]"))
        add(SlashCommand("clear", "new conversation, clear the screen", self.clear))
        add(SlashCommand("restore", "bring back a previous session of this folder", self.restore, "[list|N]", self.complete_restore))
        add(SlashCommand("undo", "undo the file changes of the last turn", self.undo))
        add(SlashCommand("plan", "plan mode on or off: the agent may only read, and answers with a plan", self.plan, "[on|off]"))
        add(SlashCommand("init", "create the project instructions file", self.init))
        add(SlashCommand("todo", "show the agent's checklist", self.todo))
        add(SlashCommand("permissions", "show or set the auto-accept level, the \"always\" answers", self.permissions, "[0|1|2|forget]", self.complete_permissions))
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
                self.app.model_changed(str(health.get("model") or self.app.model), str(health.get("provider") or ""))
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

    # -- /remind, /reminders, /unremind: shown on your own clients ------------------------------ #

    def remind(self, arguments: str) -> CommandResult:
        targets, arguments = take_targets(arguments)
        try:
            due, repeat, text = parse_remind(arguments, datetime.now().astimezone())
            reminder = self.app.client.add_reminder(due.isoformat(timespec="seconds"), text, repeat, targets)
        except (ValueError, ClaraError) as error:
            return self._error(str(error))
        when = local_time(reminder["due_at"])
        again = f", then {reminder['repeat']}" if reminder["repeat"] else ""
        where = ", ".join(reminder.get("targets") or []) or "all your clients"
        return CommandResult(
            self.renderer.text(f"Reminder {reminder['id']} set for {when}{again}, shown on {where}.", "green")
        )

    def reminders(self, arguments: str) -> CommandResult:
        try:
            found = self.app.client.reminders()
        except ClaraError as error:
            return self._error(str(error))
        if not found:
            return self._info("No reminder waiting.")
        table = Table(title="Reminders", title_justify="left")
        table.add_column("#", justify="right")
        table.add_column("When")
        table.add_column("Repeat")
        table.add_column("Where")
        table.add_column("Text", overflow="fold")
        for reminder in found:
            where = ", ".join(reminder.get("targets") or []) or "everywhere"
            table.add_row(str(reminder["id"]), local_time(reminder["due_at"]), reminder["repeat"], where, reminder["text"])
        return CommandResult(self.renderer.render(table))

    def unremind(self, arguments: str) -> CommandResult:
        if not arguments.strip().isdigit():
            return self._error("Usage: /unremind ID   (the number shown by /reminders)")
        try:
            self.app.client.cancel_reminder(int(arguments))
        except ClaraError as error:
            return self._error(str(error))
        return CommandResult(self.renderer.text("Reminder cancelled.", "green"))

    def complete_remind(self, arguments: str) -> Iterator[Completion]:
        words = arguments.split(" ")
        after_repeat = len(words) == 2 and words[0].lower() in REPEATS
        if len(words) > 2 or (len(words) == 2 and not after_repeat):
            return
        last = words[-1].lower()
        options = {"tomorrow": "tomorrow at HH:MM"}
        if not after_repeat:
            options = {"daily": "every day", "weekly": "every week", "monthly": "every month", **options}
        for word, meta in options.items():
            if word.startswith(last):
                yield Completion(word, start_position=-len(last), display_meta=meta)

    def complete_unremind(self, arguments: str) -> Iterator[Completion]:
        if " " in arguments:
            return
        try:
            found = self.app.client.reminders()
        except ClaraError:
            return
        for reminder in found:
            if str(reminder["id"]).startswith(arguments):
                yield Completion(str(reminder["id"]), start_position=-len(arguments), display_meta=reminder["text"][:60])

    # -- /notify-after: when a finished task notifies you ------------------------------------- #

    def notify_after(self, arguments: str) -> CommandResult:
        word = arguments.strip().lower()
        try:
            if not word:
                settings = self.app.client.settings()
            elif word == "default":
                settings = self.app.client.set_notify_after(None)
            elif word in ("off", "never"):
                settings = self.app.client.set_notify_after(0)
            elif word.isdigit():
                settings = self.app.client.set_notify_after(int(word))
            else:
                return self._error("Usage: /notify-after [SECONDS|off|default]")
        except ClaraError as error:
            return self._error(str(error))

        def words(seconds: int) -> str:
            return "never" if seconds == 0 else f"after {seconds} s of work"

        own, default = settings["notify_after"], settings["notify_after_default"]
        if own is None:
            text = f"You are notified {words(default)} when a task is done (the server's default)."
        else:
            text = f"You are notified {words(own)} when a task is done (the server's default: {words(default)})."
        return CommandResult(self.renderer.text(text, "green" if word else "dim"))

    def complete_notify_after(self, arguments: str) -> Iterator[Completion]:
        if " " in arguments:
            return
        for word, meta in {"off": "never notified", "default": "the server's delay"}.items():
            if word.startswith(arguments.lower()):
                yield Completion(word, start_position=-len(arguments), display_meta=meta)

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
        self.app.session.pending_notes.append(
            "[The user undid the file changes of your last turn that changed files (/undo):\n"
            + "\n".join(f"- {line}" for line in report)
            + "\nWhat you remember of these files is outdated: read them again before relying on it.]"
        )
        return CommandResult(self.renderer.text("\n".join(report), "green"))

    def plan(self, arguments: str) -> CommandResult:
        word = arguments.strip().lower()
        if word not in ("", "on", "off"):
            return self._error("Usage: /plan [on|off]   (no argument: switch)")
        on = (not self.app.plan_mode) if not word else word == "on"
        self.app.set_plan_mode(on)
        if on:
            message = "Plan mode on: the agent may only read, and answers with a plan. /plan again to let it act."
        else:
            message = "Plan mode off: the agent may change files and run commands again (with your permission)."
        return CommandResult(self.renderer.text(message, "green"))

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

    PERMISSIONS_USAGE = "Usage: /permissions [0|1|2 | forget]   (forget: ask again for what you answered \"always\")"

    def permissions(self, arguments: str) -> CommandResult:
        gate = self.app.tool_context.gate
        word = arguments.strip().lower()
        if word == "forget":
            gate.rules.forget()
            return CommandResult(self.renderer.text("The \"always\" answers are forgotten: those calls ask again.", "green"))
        if word:
            try:
                level = int(word)
                PermissionLevel(level)
            except ValueError:
                return self._error(self.PERMISSIONS_USAGE)
            gate.auto_level = level
            self.app.save_session()
        zone = self.app.tool_context.zone
        lines = [
            f"Auto-accept level {gate.auto_level}: {permission_label(gate.auto_level)}",
            f"Free zone (no question asked for files there): {zone.describe()}",
        ]
        if zone.active:
            lines.append("  except changes to .git, .vscode, .venv, .env files and the project file, which ask")
        allowed = gate.rules.listed()
        lines.append("Always allowed: " + (", ".join(allowed) if allowed else "nothing (answer a or p to a question)"))
        return CommandResult(self.renderer.text("\n".join(lines)))

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
        if "forget".startswith(arguments.lower()):
            yield Completion("forget", start_position=-len(arguments), display_meta="ask again for the \"always\" answers")
