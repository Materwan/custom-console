"""`ask_user`: the agent asks the user a question with options."""

from typing import Any, Callable, Dict, List, Union

from ..permissions import PermissionLevel
from ..questions import Choice
from ..results import ToolResult
from .base import ToolContext, guarded

MAX_OPTIONS = 9


def parse_options(raw: List[Union[str, Dict[str, Any]]]) -> List[Choice]:
    """Options as the model writes them: {"label", "description"} objects, or plain strings."""
    choices: List[Choice] = []
    for entry in raw or []:
        if isinstance(entry, str):
            label, description = entry.strip(), ""
        elif isinstance(entry, dict):
            label = str(entry.get("label") or entry.get("title") or entry.get("option") or "").strip()
            description = str(entry.get("description") or "").strip()
        else:
            raise ValueError(f"Each option must be an object with 'label' and 'description', got {entry!r}.")
        if not label:
            raise ValueError("Every option needs a non-empty 'label'.")
        choices.append(Choice(label, description))
    if len(choices) > MAX_OPTIONS:
        raise ValueError(f"At most {MAX_OPTIONS} options: keep the most useful ones.")
    if len({choice.label for choice in choices}) != len(choices):
        raise ValueError("Two options have the same label.")
    return choices


def ask_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    if ctx.ask_user is None:  # nobody to ask (no terminal UI)
        return []

    @guarded(ctx, PermissionLevel.NONE)
    def ask_user(
        question: str,
        options: List[Dict[str, str]],
        multiple: bool = False,
        allow_other: bool = True,
    ) -> ToolResult:
        """Ask the user a question and wait for the answer. Use it when you need a decision
        or a preference that you cannot find out yourself, instead of guessing; do not use
        it for permission to run a tool (the user is asked anyway).

        Args:
            question: the question, in one or two sentences.
            options: 2 to 6 possible answers (at most 9), each {"label": "short answer", "description":
                "what choosing it implies"}. May be empty when allow_other is true.
            multiple: true when the user may pick several options.
            allow_other: true to let the user type an answer of their own instead.
        """
        if not question.strip():
            raise ValueError("The question is empty.")
        choices = parse_options(options)
        if not choices and not allow_other:
            raise ValueError("Give options, or set allow_other to true.")

        answer = ctx.ask_user(question.strip(), choices, multiple, allow_other)
        if answer is None:
            return ToolResult.ok(
                {"answered": False, "note": "The user skipped the question. Do not ask it again."},
                summary="skipped",
                detail=question,
            )
        data: Dict[str, Any] = {"answered": True, "selected": answer.selected}
        if answer.other:
            data["other_answer"] = answer.other
        shown = answer.describe()
        return ToolResult.ok(data, summary=f"→ {shown}", detail=f"{question}\n→ {shown}")

    return [ask_user]
