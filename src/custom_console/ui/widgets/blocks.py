"""The pieces of the transcript: your messages, answers, thinking, tool lines, notes, sub-agents, and the two inline prompts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Static

from ...core.tools.base import Question
from ...core.turn import Prompt

TOOL_BODY_LINES = 30


def plain(text: str, style: str = "") -> Text:
    """Text taken literally: no markup is read, so a tool result with `[bold]` in it is shown as it is."""
    return Text(text, style=style)


class UserBlock(Static):
    def __init__(self, text: str, notes: Sequence[str] = ()):
        body = Text(text)
        for note in notes:
            body.append("\n+ " + note, style="dim")
        self.body = body
        super().__init__(self.gutter_text())

    def gutter_text(self) -> Text:
        return Text.assemble(("| ", "bold cyan"), self.body)

    def snapshot(self) -> RenderableType | None:
        return Padding(self.gutter_text(), (1, 0, 0, 0))


class NoteBlock(Static):
    """One line (or a few) of information; `level` is dim, warn, error or ok. Errors open with `!!`, as tool lines do."""

    STYLES = {"dim": "dim", "warn": "yellow", "error": "bold red", "ok": "green"}
    PREFIXES = {"error": "!! "}

    def __init__(self, text: str, level: str = "dim"):
        self.message, self.level = text, level
        super().__init__(self.styled(), classes=level)

    def styled(self) -> Text:
        return Text(self.PREFIXES.get(self.level, "") + self.message, style=self.STYLES.get(self.level, ""))

    def snapshot(self) -> RenderableType | None:
        return self.styled()


class OutputBlock(Static):
    """The output of a command (`!git status`, `/ls`)."""

    def __init__(self, content: Text):
        self.shown = content
        super().__init__(content)

    def snapshot(self) -> RenderableType | None:
        return Padding(self.shown, (0, 0, 0, 2))


class AnswerBlock(Vertical):
    """An answer being written. Paragraphs that are complete are frozen as plain text; only the open one is redrawn, in
    batches (`flush`), so a long answer costs no more per frame than a short one. `finish` replaces it all by Markdown, once."""

    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self._frozen_to = 0
        self._stale = False
        self._live = Static("", classes="live")
        self.complete = False

    def compose(self) -> ComposeResult:
        yield self._live

    def feed(self, text: str) -> None:
        self.text += text
        self._stale = True

    def flush(self) -> None:
        if not self._stale or self.complete:
            return
        if not self._live.is_attached:
            return  # not drawn yet: the next batch
        self._stale = False
        cut = self.text.rfind("\n\n", self._frozen_to)
        if cut != -1:
            frozen = self.text[self._frozen_to:cut]
            self._frozen_to = cut + 2
            if frozen.strip():
                self.mount(Static(plain(frozen)), before=self._live)
        self._live.update(plain(self.text[self._frozen_to:]))

    def finish(self) -> None:
        """Called once, when the answer is complete. Safe to call twice."""
        if self.complete:
            return
        self.complete = True
        self.call_later(self._render_markdown)

    def snapshot(self) -> RenderableType | None:
        if not self.complete:
            return None  # still being written
        if not self.text.strip():
            return Text("")
        if not self.query("Static.markdown"):
            return None  # not drawn as Markdown yet
        return Padding(RichMarkdown(self.text, code_theme="ansi_dark"), (1, 0, 0, 0))

    async def _render_markdown(self) -> None:
        await self.remove_children()
        if self.text.strip():  # one widget per message, however long: the transcript's layout cost follows the widget count
            await self.mount(Static(RichMarkdown(self.text, code_theme="ansi_dark"), classes="markdown"))


class ThinkingBlock(Collapsible):
    def __init__(self) -> None:
        self._body = Static("")
        super().__init__(self._body, title="Thinking...", collapsed=True)
        self.text = ""
        self.done = False
        self._stale = False

    def feed(self, text: str) -> None:
        self.text += text
        self._stale = True

    def flush(self) -> None:
        if self._stale:
            self._stale = False
            self._body.update(plain(self.text.strip()))

    def finish(self) -> None:
        self.flush()
        self.title = f"Thought ({len(self.text.split())} words)"
        self.done = True

    def snapshot(self) -> RenderableType | None:
        return Text("  " + str(self.title), style="dim") if self.done else None


def brief(arguments: dict[str, Any]) -> str:
    for key in ("path", "command", "pattern", "glob", "query", "url", "name", "app"):
        if arguments.get(key):
            return str(arguments[key]).replace("\n", " ")[:80]
    return ""


def diff_body(preview: str) -> Syntax:
    return Syntax(preview, "diff", theme="ansi_dark", word_wrap=True, background_color="default")


class ToolBlock(Collapsible):
    def snapshot(self) -> RenderableType | None:
        return Text("  " + str(self.title), style="dim")


def tool_block(name: str, arguments: dict[str, Any], ok: bool, result: str, seconds: float, preview: str = "",
               server: bool = False) -> ToolBlock:
    """One line per tool call; open it to see the diff (when there is one) and the result."""
    mark = "ok" if ok else "!!"
    title = f"{mark} {name.removeprefix('local_')} {brief(arguments)}  {seconds:.1f}s".rstrip() if seconds else \
        f"{mark} {name} {brief(arguments)}".rstrip()
    shown = result.strip().splitlines()
    cut = len(shown) - TOOL_BODY_LINES
    body_text = "\n".join(shown[:TOOL_BODY_LINES]) + (f"\n[... {cut} more lines ...]" if cut > 0 else "")
    children: list[Static] = []
    if preview.strip():
        children.append(Static(diff_body(preview)))
    children.append(Static(plain(body_text or "(no output)", "" if ok else "red")))
    block = ToolBlock(*children, title=title, collapsed=True)
    block.add_class("server" if server else "local")
    return block


class PendingTool(Static):
    """A server tool that has started and not finished."""

    def __init__(self, name: str, arguments: dict[str, Any]):
        super().__init__(plain(f"... {name} {brief(arguments)}", "dim"))
        self.tool = name


class SubagentBlock(Collapsible):
    def __init__(self, label: str):
        self._log = Static("")
        super().__init__(self._log, title=f"Sub-agent: {label} ...", collapsed=True)
        self.label = label
        self.done = False
        self.lines: list[str] = []

    def add_line(self, text: str) -> None:
        self.lines.append(text)
        self._log.update(plain("\n".join(self.lines[-60:])))

    def finish(self, ok: bool, summary: str) -> None:
        self.title = f"{'ok' if ok else '!!'} Sub-agent: {self.label} - {summary}"
        self.done = True

    def snapshot(self) -> RenderableType | None:
        return Text("  " + str(self.title), style="dim") if self.done else None


class ApprovalBlock(Vertical, can_focus=True):
    """A permission prompt, inline. The same for Clara's own approvals and the console's local ones."""

    KEYS = {"o": "allow_once", "a": "allow_always", "r": "refuse_once", "n": "refuse_always", "escape": "refuse_once"}
    NOT_FOR_TRUST = ("allow_always", "refuse_always")  # trusting a folder is a one-time question: `a` and `n` do nothing there
    # After `a` on a command a pattern could cover: keep the pattern, keep only this exact command, or allow once.
    CONFIRM_KEYS = {"y": "allow_pattern", "e": "allow_always", "n": "allow_once"}

    def __init__(self, prompt: Prompt):
        super().__init__()
        self.prompt = prompt
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.trust = prompt.tool == "trust"
        self.outcome = ""
        self.stage = "ask"  # "ask"; "waiting" while the model proposes a pattern; "confirm" while the person decides on it
        self._keys: Static | None = None

    def compose(self) -> ComposeResult:
        p = self.prompt
        who = "Clara asks" if p.kind == "server" else "Allow this?"
        head = Text.assemble((who + "  ", "bold yellow"), (p.summary, "bold"))
        yield Static(head)
        if p.reason:
            yield Static(plain(p.reason, "dim"))
        if p.command:
            yield Static(plain("$ " + p.command, "cyan"))
        for shown in p.paths:
            line = f"{shown.mode}: {shown.virtual}" + (f"  (really {shown.real})" if shown.redirected else "")
            yield Static(plain(line, "dim"))
        if p.floor:
            yield Static(plain("Always asks: " + "; ".join(p.floor), "yellow"))
        if p.tainted:
            yield Static(plain("This turn read outside content, so it asks whatever the rules say.", "yellow"))
        if p.preview.strip():
            yield Static(diff_body(p.preview) if "\n@@" in p.preview or p.preview.startswith(("---", "+++", "@@")) else
                         plain(p.preview))
        if not p.undoable and p.tool in ("local_run",):
            yield Static(plain("What a command changes cannot be undone with /undo.", "yellow"))
        self._keys = Static(self._keys_line())
        yield self._keys

    def _proposable(self) -> bool:
        p = self.prompt
        return not self.trust and p.kind == "local" and p.proposal is not None and p.proposal.possible

    def _refresh(self) -> None:
        if self._keys is not None:
            self._keys.update(self._keys_line())

    def _keys_line(self) -> Text:
        if self.trust:
            return Text.assemble(("[o]", "bold green"), " trust this folder   ", ("[r]", "bold red"), " read only")
        p = self.prompt
        if self.stage == "waiting":
            return Text("Looking for a pattern to keep ...", style="dim")
        if self.stage == "confirm":
            shown = ", ".join(" ".join(pattern) + ":*" for pattern in (p.proposal.patterns if p.proposal else ()))
            line = Text.assemble(("Always allow in this folder:  ", "bold"), (shown, "cyan"), "\n",
                                 ("[y]", "bold green"), " yes   ")
            if p.can_remember:
                line.append_text(Text.assemble(("[e]", "bold green"), " only this exact command   "))
            return line.append_text(Text.assemble(("[n]", "bold green"), " just this once   ", ("[esc]", "bold"), " back"))
        line = Text.assemble(("[o]", "bold green"), " allow once   ", ("[a]", "bold green"), " allow always   ",
                             ("[r]", "bold red"), " refuse once   ", ("[n]", "bold red"), " refuse always")
        if p.kind == "local" and not p.can_remember and not self._proposable():
            line.append(f"   ('allow always' and 'refuse always' are not kept: {p.why_not_remembered or 'asked every time'})",
                        style="dim")
        if p.seconds_left:
            line.append(f"   ({p.seconds_left:.0f} s to answer)", style="dim")
        return line

    def on_mount(self) -> None:
        self.focus()
        self.scroll_visible()

    def on_key(self, event: events.Key) -> None:
        if self.stage != "ask":
            event.stop()
            event.prevent_default()
            if self.stage == "confirm":
                self._on_confirm_key(event.key)
            elif event.key == "escape":  # tired of waiting for the model: back to the first question
                self.stage = "ask"
                self._refresh()
            return
        answer = self.KEYS.get(event.key)
        if answer is None or (self.trust and answer in self.NOT_FOR_TRUST):
            return
        event.stop()
        event.prevent_default()
        if answer == "allow_always" and self._proposable():
            self.stage = "waiting"
            self._refresh()
            self.run_worker(self._propose(), exclusive=True)
            return
        self.resolve(answer)

    def _on_confirm_key(self, key: str) -> None:
        if key == "escape":
            self.stage = "ask"
            self._refresh()
            return
        answer = self.CONFIRM_KEYS.get(key)
        if answer is not None and (answer != "allow_always" or self.prompt.can_remember):
            self.resolve(answer)

    async def _propose(self) -> None:
        """Ask for the patterns (the model, for a command the table does not know), then let the person confirm them."""
        proposal = self.prompt.proposal
        patterns = await proposal.resolve() if proposal is not None else ()
        if self.future.done() or self.stage != "waiting":
            return
        if patterns:
            self.stage = "confirm"
            self._refresh()
        else:  # nothing to propose: "always" means the exact command, as it did before patterns
            self.stage = "ask"
            self.resolve("allow_always")

    def resolve(self, answer: str, note: str = "") -> None:
        if self.future.done():
            return
        self.future.set_result(answer)
        words = {"allow_once": "allowed once", "allow_always": "allowed, always", "allow_pattern": "allowed, always (pattern)",
                 "refuse_once": "refused", "refuse_always": "refused, never again"}
        self.add_class("done")
        self.can_focus = False
        self.call_later(self._collapse, note or words.get(answer, answer))

    def close_with_note(self, note: str) -> None:
        """Answered somewhere else, or no longer needed: close without an answer."""
        if self.future.done():
            return
        self.future.cancel()
        self.add_class("done")
        self.can_focus = False
        self.call_later(self._collapse, note)

    async def _collapse(self, outcome: str) -> None:
        self.outcome = f"{outcome}: {self.prompt.summary}"
        await self.remove_children()
        await self.mount(Static(plain(self.outcome)))

    def snapshot(self) -> RenderableType | None:
        return Text(self.outcome, style="dim") if self.future.done() and self.outcome else None


class QuestionBlock(Vertical):
    """A question from the model (`local_ask_user`): answered by typing in the prompt box, or by the option's number."""

    def __init__(self, question: Question):
        super().__init__()
        self.question = question
        self.outcome = ""
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("Question  ", "bold cyan"), (self.question.text, "bold")))
        for number, option in enumerate(self.question.options, 1):
            yield Static(plain(f"  {number}. {option}"))
        hint = "Type a number or your own answer below, then Enter." if self.question.options else \
            "Type your answer below, then Enter."
        yield Static(plain(hint, "dim"))

    def answer(self, text: str) -> None:
        if self.future.done():
            return
        options = self.question.options
        chosen = options[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(options) else text
        self.future.set_result(chosen)
        self.add_class("done")
        self.call_later(self._collapse, chosen)

    async def _collapse(self, chosen: str) -> None:
        self.outcome = f"{self.question.text} -> {chosen}"
        await self.remove_children()
        await self.mount(Static(plain(self.outcome)))

    def snapshot(self) -> RenderableType | None:
        return Text(self.outcome, style="dim") if self.future.done() and self.outcome else None


class HistoryBlock(Static):
    """Old, finished messages folded into one widget. Textual lays out every widget again whenever one changes size, so the cost
    of a streamed frame follows the number of widgets in the transcript; folding keeps that number bounded (PERF-1)."""

    def __init__(self) -> None:
        super().__init__("")
        self.items: list[RenderableType] = []

    def absorb(self, items: Sequence[RenderableType]) -> None:
        self.items.extend(items)
        self.update(Group(*self.items))
