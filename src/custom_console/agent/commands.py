"""Slash commands of the agent console (`/model`, `/new`, `/permissions`, `/tokens`).

They only concern the person talking to the agent: which model answers, which
tools run unasked, a fresh conversation, what the conversation costs. Managing
the Ollama server itself (`ai list`, `ai start`) stays in the shell.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from rich.table import Table
from rich.text import Text

from ..llm.ollama import ModelInfo, OllamaUnavailableError, format_size
from .permissions import PermissionLevel
from .ui import CommandOutput, SlashCommand

if TYPE_CHECKING:  # pragma: no cover
    from .console import AgentConsole

LEVEL_HELP = {
    PermissionLevel.NONE: "always ask",
    PermissionLevel.READ: "reads run unasked",
    PermissionLevel.WRITE: "everything runs unasked",
}


def is_cloud(name: str) -> bool:
    return name.endswith("cloud")  # served remotely: nothing to load locally


def pick_model(models: List[ModelInfo], reference: str) -> Optional[ModelInfo]:
    """A model by its number in the `/model` list, or by name (`llama3` matches `llama3:8b`)."""
    reference = reference.strip()
    if reference.isdigit():
        index = int(reference) - 1
        return models[index] if 0 <= index < len(models) else None
    base, _, tag = reference.partition(":")
    for model in models:
        if model.name == reference or (not tag and model.name.split(":")[0] == base):
            return model
    return None


class AgentCommands:
    def __init__(self, console: "AgentConsole"):
        self.console = console
        self._names: List[str] = []  # model names for completion (filled by /model)

    def all(self) -> List[SlashCommand]:
        return [
            SlashCommand(
                "model", "[number|name|default]", "show the installed models, or answer with another one",
                self.model, lambda: [*self._names, "default"],
            ),
            SlashCommand("new", "", "start a fresh conversation (the screen is cleared)", self.new),
            SlashCommand(
                "permissions", "[0|1|2]", "which tools run without asking", self.permissions,
                lambda: ["0", "1", "2"],
            ),
            SlashCommand("tokens", "", "tokens used by this conversation", self.tokens),
        ]

    # -- /model ---------------------------------------------------------------- #

    def _installed(self) -> List[ModelInfo]:
        try:
            models = self.console.ollama.installed()
        except OllamaUnavailableError as error:
            raise RuntimeError(str(error)) from error
        self._names = [model.name for model in models]
        return models

    def model(self, args: str) -> CommandOutput:
        console = self.console
        models = self._installed()
        if not args:
            return self._model_table(models)

        wanted = console.settings.default_model if args.lower() == "default" else args
        model = pick_model(models, wanted)
        if model is None:
            return Text(f"No installed model matches {args!r}. /model lists them.", style="red")
        if model.capabilities and "tools" not in model.capabilities:
            return Text(f"{model.name} cannot call tools, which the agent needs.", style="red")
        if model.name == console.model:
            return Text(f"Already answering with {model.name}.")

        if not is_cloud(model.name) and not console.ollama.is_running(model.name):
            try:
                console.ollama.start(model.name)  # loads it now instead of at the next question
            except OllamaUnavailableError as error:
                return Text(str(error), style="red")
        console.switch_model(model.name)
        return Text(f"Now answering with {model.name}.", style="green")

    def _model_table(self, models: List[ModelInfo]) -> Table:
        console = self.console
        try:
            running = {model.name for model in console.ollama.running()}
        except OllamaUnavailableError:
            running = set()
        table = Table(title="Models (/model <number|name> to switch)", title_justify="left")
        for column in ("", "#", "Model", "Size", "State", "Capabilities"):
            table.add_column(column, no_wrap=True)
        for number, model in enumerate(models, 1):
            state = "cloud" if is_cloud(model.name) else "loaded" if model.name in running else ""
            usable = not model.capabilities or "tools" in model.capabilities
            table.add_row(
                "●" if model.name == console.model else "",
                str(number),
                Text(model.name, style="" if usable else "dim strike"),
                format_size(model.size) if not is_cloud(model.name) else "",
                state,
                ", ".join(model.capabilities) if usable else "no tools: unusable",
            )
        return table

    # -- /new /permissions /tokens --------------------------------------------- #

    def new(self, args: str) -> CommandOutput:
        self.console.new_conversation()
        return None

    def permissions(self, args: str) -> CommandOutput:
        gate = self.console.tool_context.gate
        if args:
            if args not in ("0", "1", "2"):
                return Text("Use /permissions 0, 1 or 2.", style="red")
            gate.auto_level = int(args)
        lines = [
            f"{'●' if gate.auto_level == level else ' '} {int(level)}  {LEVEL_HELP[level]}"
            for level in PermissionLevel
        ]
        return Text("\n".join(lines))

    def tokens(self, args: str) -> CommandOutput:
        session = self.console.session
        if not session.turns:
            return Text("No answer yet.")
        last, total = session.last, session.totals
        average = total.total_tokens // session.turns
        return Text(
            f"last answer: {last.input_tokens} in + {last.output_tokens} out\n"
            f"conversation: {session.turns} answers, {total.input_tokens} in + {total.output_tokens} out "
            f"= {total.total_tokens} tokens ({average} per answer)"
        )
