"""What the agent screen shows above its input line while it waits for the user: a question
(yes/no, a tool's permission, a line of text), a checklist menu (``/tools``) or the agent's own
question with options (``ask_user``). Each holds its state and the future its answer resolves;
`ui.AgentScreen` draws them and routes the keys.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .permissions import Decision, is_yes, parse_decision, rule_label
from .questions import Answer, Choice


def parse_answer(text: str, default: bool) -> bool:
    """Answer to a yes/no question: an empty line means the default."""
    return default if not text.strip() else is_yes(text)


@dataclass
class Question:
    """A yes/no question (a confirmation), or with `tool` a tool's permission question, which
    needs an explicit answer and may be answered "always" (see `permissions.parse_decision`)."""

    info: str
    future: Optional["asyncio.Future"] = None  # made by the screen, on its event loop
    default: bool = True
    tool: bool = False
    rule: Optional[str] = None  # what "always" covers; None: the call cannot be approved for good
    project: bool = False  # "p" (always in this project) is offered

    def resolve(self, answer) -> None:
        if self.future is not None and not self.future.done():
            self.future.set_result(answer)

    def answer(self, text: str):
        """What `text` answers (a bool, or a Decision for a tool), None when nothing was answered yet."""
        if not self.tool:
            return parse_answer(text, self.default)
        return parse_decision(text, can_remember=self.rule is not None)

    def refuse(self):
        return Decision(False) if self.tool else False

    def hint(self) -> str:
        if not self.tool:
            return ""
        parts = ["y yes"]
        if self.rule is not None:
            parts.append(f"a always for {rule_label(self.rule)} this session")
            if self.project:
                parts.append("p … in this project")
        parts.append("n no (n <why> tells the agent)")
        return "  " + " · ".join(parts)

    def prompt(self) -> str:
        if self.tool:
            keys = "y|n"
            if self.rule is not None:
                keys = "y|a|p|n" if self.project else "y|a|n"
            return f"Allow? ({keys}) > "
        return "Accept (Y|n) > " if self.default else "Accept (y|N) > "


@dataclass
class TextQuestion:
    """A question answered with free text (an API key, when `secret`: typed masked, never kept in the history)."""

    info: str
    future: "asyncio.Future[Optional[str]]"
    secret: bool = False

    def resolve(self, answer: Optional[str]) -> None:
        if not self.future.done():
            self.future.set_result(answer)


@dataclass
class MenuItem:
    """A line of a checklist menu (see `AgentScreen.ask_menu`)."""

    key: str
    label: str
    detail: str = ""
    group: str = ""  # items of a group are listed together under its heading
    checked: bool = True


@dataclass
class Menu:
    title: str
    items: List[MenuItem]
    future: "asyncio.Future[Optional[Dict[str, bool]]]"
    cursor: int = 0
    scroll: int = 0

    def rows(self) -> List[Tuple[str, object]]:
        """Group headings and items, in display order."""
        rows: List[Tuple[str, object]] = []
        previous = None
        for item in self.items:
            if item.group and item.group != previous:
                rows.append(("group", item.group))
            previous = item.group
            rows.append(("item", item))
        return rows

    def members(self, group: str) -> List[MenuItem]:
        return [item for item in self.items if item.group == group]

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(len(self.rows()) - 1, self.cursor + delta))

    def toggle(self) -> None:
        kind, target = self.rows()[self.cursor]
        if kind == "item":
            target.checked = not target.checked  # type: ignore[union-attr]
        else:
            members = self.members(target)  # type: ignore[arg-type]
            state = not all(item.checked for item in members)
            for item in members:
                item.checked = state

    def toggle_all(self) -> None:
        state = not all(item.checked for item in self.items)
        for item in self.items:
            item.checked = state

    def result(self) -> Dict[str, bool]:
        return {item.key: item.checked for item in self.items}

    def resolve(self, result: Optional[Dict[str, bool]]) -> None:
        if not self.future.done():
            self.future.set_result(result)


@dataclass
class ChoiceQuestion:
    """A question of the agent with options (see `AgentScreen.ask_choice`).

    The row after the options, when `allow_other`, is "another answer": what
    the user types in the input line.
    """

    question: str
    options: List[Choice]
    multiple: bool
    allow_other: bool
    future: "asyncio.Future[Optional[Answer]]"
    cursor: int = 0
    checked: Set[int] = field(default_factory=set)
    scroll: int = 0  # first row shown, when the options do not fit
    visible: int = 0  # rows shown at the last drawing

    @property
    def other_row(self) -> int:
        return len(self.options)

    def on_other(self) -> bool:
        return self.allow_other and self.cursor == self.other_row

    def move(self, delta: int) -> None:
        last = self.other_row if self.allow_other else len(self.options) - 1
        self.cursor = max(0, min(last, self.cursor + delta))

    def toggle(self) -> None:
        if self.cursor < len(self.options):
            self.checked ^= {self.cursor}

    def answer(self, typed: str) -> Optional[Answer]:
        """The answer Enter gives, or None when there is nothing to send yet."""
        typed = typed.strip() if self.allow_other else ""
        if self.multiple:
            selected = [option.label for index, option in enumerate(self.options) if index in self.checked]
            if not selected and not typed and self.cursor < len(self.options):
                selected = [self.options[self.cursor].label]
            return Answer(selected, typed) if selected or typed else None
        if self.on_other():
            return Answer([], typed) if typed else None
        return Answer([self.options[self.cursor].label]) if self.options else None

    def resolve(self, answer: Optional[Answer]) -> None:
        if not self.future.done():
            self.future.set_result(answer)
