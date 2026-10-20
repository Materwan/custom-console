"""The full-screen agent console (Textual).

A transcript on top, a status line, and one prompt box at the bottom. This module draws and reads keys; the agent
(`core.agent.Agent`) and the slash commands decide and act. Streamed text is drawn in batches (15 times a second), complete
paragraphs are frozen, and a finished answer is rendered once as Markdown and never redrawn (PERF-1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Sequence
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..clara import ClaraError
from ..clara.events import (
    ApprovalRequested,
    Compacted,
    Discarded,
    Done,
    ErrorEvent,
    Event,
    FileNote,
    Quiz,
    Retrying,
    Thinking,
    Token,
    ToolCall,
    ToolDone,
    ToolRequests,
    ToolStart,
    TurnStarted,
    Usage,
    Warning,
)
from ..clara.types import Attachment, Message
from ..commands.base import Line, Shell
from ..commands.completion.engine import CompletionEngine
from ..commands.history import MessageHistory
from ..commands.slash import build_slash_registry
from ..commands.slash.base import SlashContext, SlashSpec
from ..commands.slash.router import Routed, route, run_slash, unknown_message
from ..commands.slash.shellbridge import run_shell_command
from ..core.agent import Agent, AgentError
from ..core.attachments import human_size, pasted_image
from ..core.options import AgentOptions
from ..core.runtime import Runtime
from ..core.tools.base import Question
from ..core.turn import Prompt
from ..platform import CancelToken, DesktopError
from .widgets.blocks import (
    AnswerBlock,
    ApprovalBlock,
    HistoryBlock,
    NoteBlock,
    OutputBlock,
    PendingTool,
    QuestionBlock,
    SubagentBlock,
    ThinkingBlock,
    UserBlock,
    plain,
    tool_block,
)
from .widgets.picker import PickerScreen
from .widgets.prompt import PromptBox
from .widgets.status import StatusFacts, StatusLine
from .widgets.viewer import ViewerScreen

log = logging.getLogger("custom_console.agent_app")
FLUSH_INTERVAL = 1 / 12
MAX_BLOCKS = 80  # widgets in the transcript before the oldest finished ones are folded into one
KEEP_BLOCKS = 40  # the newest ones that stay as they are
TRIM_RETRY_SECONDS = 1.0  # after finding the front of the transcript still at work, look again no sooner than this
STATUS_INTERVAL = 0.5


def common_prefix(words: Sequence[str]) -> str:
    """The longest start all the words share."""
    if not words:
        return ""
    low, high = min(words), max(words)
    for index, char in enumerate(low):
        if char != high[index]:
            return low[:index]
    return low


class AppUI:
    """What the agent and the turn runner call; each call lands on the app."""

    def __init__(self, app: AgentApp):
        self.app = app

    def show(self, event: Event) -> None:
        self.app.handle_event(event)

    async def confirm(self, prompt: Prompt) -> str:
        return await self.app.ask_permission(prompt)

    async def ask_user(self, question: Question) -> str:
        return await self.app.ask_question(question)

    def approval_closed(self, approval_id: int, note: str) -> None:
        self.app.close_approval(approval_id, note)

    def tool_finished(self, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None:
        self.app.add(tool_block(call.name, call.arguments, ok, result, seconds, preview))

    def note(self, text: str) -> None:
        self.app.say(text, "dim")

    def sub_started(self, label: str) -> int:
        return self.app.sub_started(label)

    def sub_line(self, number: int, text: str) -> None:
        self.app.sub_line(number, text)

    def sub_finished(self, number: int, ok: bool, summary: str) -> None:
        self.app.sub_finished(number, ok, summary)


class AgentApp(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "Clara"
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Stop", show=False, priority=True),
        Binding("ctrl+d", "leave", "Leave", show=False, priority=True),
        Binding("escape", "stop", "Stop", show=False),
    ]

    def __init__(self, runtime: Runtime, shell: Shell, options: AgentOptions):
        super().__init__()
        self.runtime, self.shell = runtime, shell
        self.ui = AppUI(self)
        self.agent = Agent(runtime, shell.vfs, shell.env, options, self.ui)
        self.slash = build_slash_registry()
        self.engine = CompletionEngine(shell, slash_commands=self.slash.pairs)
        self.history = MessageHistory(runtime.settings.data.agent_history)
        self.chips: list[Attachment] = []
        self.image_count = 0
        self.turn_running = False
        self.command_running = False
        self.turn_started = 0.0
        self.turn_model = ""
        self._answer: AnswerBlock | None = None
        self._thinking: ThinkingBlock | None = None
        self._pending_tools: list[PendingTool] = []
        self._approvals: dict[int, ApprovalBlock] = {}
        self._question: QuestionBlock | None = None
        self._permission_lock = asyncio.Lock()
        self._subagents: dict[int, SubagentBlock] = {}
        self._flow: asyncio.Future[Any] | None = None
        self._background: list[asyncio.Future[Any]] = []
        self._cands: list[Any] = []
        self._cand_start = 0
        self._complete_cancel: CancelToken | None = None
        self._frame = 0
        self._streaming = False
        self._popup_text = ""
        self._trim_retry_at = 0.0

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        with Vertical(id="bottom"):
            yield OptionList(id="popup")
            yield Static("", id="chips", classes="empty")
            yield StatusLine()
            yield PromptBox()

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def prompt_box(self) -> PromptBox:
        return self.query_one(PromptBox)

    @property
    def busy(self) -> bool:
        return self.turn_running or self.command_running

    async def on_mount(self) -> None:
        self.transcript.anchor()
        self.prompt_box.remember(self.history.messages())
        self.prompt_box.focus()
        self.set_interval(FLUSH_INTERVAL, self.flush_streams)
        self.set_interval(STATUS_INTERVAL, self.refresh_status)
        folder = self.agent.folder.virtual
        self.say(f"Talking to Clara in {folder}. Esc stops an answer; /exit leaves.", "dim")
        self.refresh_status()
        if not self.agent.trusted:
            self.run_worker(self._ask_trust(), group="trust", exit_on_error=False)
        elif warning := self.agent.project_warning():
            self.say(warning, "warn")

    async def on_unmount(self) -> None:
        self.agent.cancel()
        await self.agent.aclose()

    async def _ask_trust(self) -> None:
        folder = self.agent.folder.virtual
        prompt = Prompt("local", "trust", f"Trust {folder}?",
                        "A trusted folder lets the agent change files and run commands here (each change still asks) and "
                        "reads AGENT.md. If you say no, the agent can only read.")
        answer = await self.ask_permission(prompt)
        if answer == "allow_once":
            self.agent.trust_folder()
            self.say("Folder trusted.", "ok")
        else:
            self.say("Not trusted: Clara can only read here. Restart `ai agent` to change that.", "warn")
        if warning := self.agent.project_warning():
            self.say(warning, "warn")
        self.refresh_status()

    # ------------------------------------------------------------------ printing
    def add(self, widget: Any) -> None:
        self.transcript.mount(widget)

    def trim_transcript(self) -> None:
        """Fold the oldest finished blocks into the history block, so the number of widgets (and the cost of a frame) stays bounded."""
        children = list(self.transcript.children)
        if len(children) <= MAX_BLOCKS or time.monotonic() < self._trim_retry_at:
            return
        history = children[0] if isinstance(children[0], HistoryBlock) else None
        folded: list[tuple[Any, Any]] = []
        for child in children[1 if history else 0:len(children) - KEEP_BLOCKS]:
            snapshot = getattr(child, "snapshot", None)
            item = snapshot() if snapshot is not None else None
            if item is None:
                break  # only a stretch from the start can be folded; a block still at work stops it
            folded.append((child, item))
        if not folded:
            self._trim_retry_at = time.monotonic() + TRIM_RETRY_SECONDS  # something at the front is still at work
            return
        self._trim_retry_at = 0.0
        if history is None:
            history = HistoryBlock()
            self.transcript.mount(history, before=children[0])
        history.absorb([item for _, item in folded])
        for child, _ in folded:
            child.remove()

    def say(self, text: str, level: str = "dim") -> None:
        self.add(NoteBlock(text, level))

    def print_line(self, line: Line) -> None:
        self.add(OutputBlock(Text.assemble(*line.segments)))

    def print_error(self, message: str) -> None:
        self.say(message, "error")

    # ------------------------------------------------------------------ the status line
    def refresh_status(self) -> None:
        agent, s = self.agent, self.agent.session
        working = (time.monotonic() - self.turn_started) if self.turn_running else None
        self._frame += 1
        facts = StatusFacts(
            folder=agent.folder.virtual, title=s.title, model=s.model or self.turn_model, incognito=s.incognito,
            local_only=s.local_only, plan=s.plan_mode, trusted=agent.trusted, context_percent=s.context_percent,
            prompt_tokens=s.prompt_tokens, completion_tokens=s.completion_tokens, jobs=agent.jobs.running,
            working_seconds=working, frame=self._frame, unicode=self.shell.unicode)
        with contextlib.suppress(Exception):  # the screen may be closing
            self.query_one(StatusLine).show(facts)

    def update_chips(self) -> None:
        chips = self.query_one("#chips", Static)
        if not self.chips:
            chips.add_class("empty")
            chips.update("")
            return
        chips.remove_class("empty")
        names = ", ".join(f"{a.name} ({human_size(len(a.data) * 3 // 4)})" for a in self.chips)  # base64 back to bytes
        chips.update(plain(f"attached: {names}   (Ctrl+X removes the last)"))

    def action_drop_chip(self) -> None:
        if self.chips:
            self.chips.pop()
            self.update_chips()

    # ------------------------------------------------------------------ streaming
    def flush_streams(self) -> None:
        for block in (self._thinking, self._answer):
            if block is not None:
                block.flush()
        with contextlib.suppress(NoMatches):  # the screen may be closing
            if len(self.transcript.children) > MAX_BLOCKS:
                self.trim_transcript()

    def answer(self) -> AnswerBlock:
        if self._answer is None:
            self._answer = AnswerBlock()
            self.add(self._answer)
        return self._answer

    def thinking(self) -> ThinkingBlock:
        if self._thinking is None:
            self._thinking = ThinkingBlock()
            self.add(self._thinking)
        return self._thinking

    def close_blocks(self) -> None:
        """The text and the thinking written so far are complete: draw them for good."""
        if self._thinking is not None:
            self._thinking.finish()
            self._thinking = None
        if self._answer is not None:
            self._answer.flush()
            self._answer.finish()
            self._answer = None

    def handle_event(self, event: Event) -> None:
        if isinstance(event, TurnStarted):
            self.turn_model = event.model
            self.refresh_status()
        elif isinstance(event, Thinking):
            self.thinking().feed(event.text)
        elif isinstance(event, Token):
            if self._thinking is not None:
                self._thinking.finish()
                self._thinking = None
            self.answer().feed(event.text)
            self._streaming = True
        elif isinstance(event, Retrying):
            self.say(event.message or f"Trying again with {event.model}.", "warn")
        elif isinstance(event, ToolStart):
            self.close_blocks()
            pending = PendingTool(event.name, event.arguments)
            self._pending_tools.append(pending)
            self.add(pending)
        elif isinstance(event, ToolDone):
            self._server_tool_done(event)
        elif isinstance(event, ToolRequests):
            self.close_blocks()
        elif isinstance(event, Usage):
            self.refresh_status()
        elif isinstance(event, Compacted):
            self.say(f"Compacted: {event.before:.0f}% to {event.after:.0f}% of the context.", "ok")
        elif isinstance(event, Discarded):
            self.say("Dropped unanswered tool calls of an earlier turn: " + ", ".join(event.tools), "dim")
        elif isinstance(event, Warning):
            self.say(event.message, "warn")
        elif isinstance(event, Quiz):
            self.say(f"Clara showed a quiz ({event.title or 'untitled'}); the console cannot display it.", "warn")
        elif isinstance(event, FileNote):
            self.say(f"Clara {event.action or 'wrote'} {event.path}", "dim")
        elif isinstance(event, Done):
            if self._answer is None and event.reply.strip() and not self._streaming:
                self.answer().feed(event.reply)
            self.close_blocks()
            self._streaming = False
            self.refresh_status()
        elif isinstance(event, (ApprovalRequested, ErrorEvent)):
            pass  # a prompt opens through `confirm`; an error is told once the turn ends

    def _server_tool_done(self, event: ToolDone) -> None:
        for pending in self._pending_tools:
            if pending.tool == event.name:
                self._pending_tools.remove(pending)
                pending.remove()
                break
        self.add(tool_block(event.name, event.arguments, True, event.result, 0.0, server=True))

    # ------------------------------------------------------------------ prompts to the person
    async def ask_permission(self, prompt: Prompt) -> str:
        async with self._permission_lock:
            block = ApprovalBlock(prompt)
            if prompt.approval_id is not None:
                self._approvals[prompt.approval_id] = block
            await self.transcript.mount(block)
            self.transcript.anchor()
            try:
                return await block.future
            except asyncio.CancelledError:
                block.close_with_note("Stopped")
                raise
            finally:
                if prompt.approval_id is not None:
                    self._approvals.pop(prompt.approval_id, None)
                self.prompt_box.focus()

    def close_approval(self, approval_id: int, note: str) -> None:
        block = self._approvals.get(approval_id)
        if block is not None:
            block.close_with_note(note)

    async def ask_question(self, question: Question) -> str:
        block = QuestionBlock(question)
        self._question = block
        await self.transcript.mount(block)
        self.transcript.anchor()
        self.prompt_box.focus()
        try:
            return await block.future
        finally:
            self._question = None

    # ------------------------------------------------------------------ sub-agents
    def sub_started(self, label: str) -> int:
        block = SubagentBlock(label)
        number = len(self._subagents) + 1
        self._subagents[number] = block
        self.add(block)
        return number

    def sub_line(self, number: int, text: str) -> None:
        if (block := self._subagents.get(number)) is not None:
            block.add_line(text)

    def sub_finished(self, number: int, ok: bool, summary: str) -> None:
        if (block := self._subagents.get(number)) is not None:
            block.finish(ok, summary)

    # ------------------------------------------------------------------ keys
    def action_interrupt(self) -> None:
        if self.busy or self._question is not None:
            self.action_stop()
        elif self.prompt_box.text:
            self.prompt_box.take()
        else:
            self.say("Ctrl+D or /exit leaves.", "dim")

    def action_leave(self) -> None:
        if not self.prompt_box.text:
            self.exit_app()

    def action_stop(self) -> None:
        if self.prompt_box.popup_open:
            self.close_popup()
            return
        if not self.busy:
            return
        self.agent.cancel()
        if self._flow is not None and not self._flow.done():
            self._flow.cancel()

    def exit_app(self) -> None:
        self.agent.cancel()
        self.exit()

    # ------------------------------------------------------------------ SlashScreen
    def clear_transcript(self) -> None:
        self._answer = self._thinking = None
        self._pending_tools.clear()
        self._subagents.clear()
        self.transcript.remove_children()

    def show_answer(self, text: str) -> None:
        block = AnswerBlock()
        block.text = text
        self.add(block)
        block.finish()

    def replay(self, messages: Sequence[Message]) -> None:
        for message in messages:
            if message.role == "user":
                self.add(UserBlock(message.content))
            elif message.role == "assistant" and message.content.strip():
                self.show_answer(message.content)

    def start_turn(self, text: str, *, label: str = "", retry: bool = False) -> None:
        if not retry:
            self.add(UserBlock(label or text))
        self.transcript.anchor()
        self.turn_running = True
        self._flow = asyncio.ensure_future(self._turn_flow(text, (), retry=retry))

    def view(self, title: str, lines: Sequence[Line]) -> None:
        self.push_screen(ViewerScreen(title, [line.plain for line in lines]))

    async def pick(self, title: str, rows: Sequence[tuple[str, str]], current: str = "") -> str | None:
        chosen: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self.push_screen(PickerScreen(title, rows, current), chosen.set_result)
        return await chosen

    def status_changed(self) -> None:
        self.refresh_status()

    # ------------------------------------------------------------------ what was typed
    async def on_prompt_box_submitted(self, message: PromptBox.Submitted) -> None:
        text = message.text
        if self._question is not None and not self._question.future.done():
            self.prompt_box.take()
            self._question.answer(text.strip())
            return
        await self.submit(text)

    async def submit(self, text: str) -> None:
        routed = route(text, self.slash, self.shell.registry)
        if routed.kind == "empty":
            return
        spec = self.slash.get(routed.name) if routed.kind == "slash" else None
        if self.busy and not (spec is not None and spec.busy_ok):
            self.say("An answer or a command is still running. Esc stops it.", "warn")
            return
        self.prompt_box.take()
        if self.history.add_message(text):
            self.prompt_box.remember(self.history.messages())
        self.close_popup()
        if routed.kind == "message":
            self.command_running = True
            self._flow = asyncio.ensure_future(self._message_flow(routed.text))
        elif routed.kind == "bang":
            self._run_in_background(self._bang_flow(routed.text))
        elif routed.kind == "slash" and spec is not None:
            self.add(UserBlock(text))
            self._run_in_background(self._slash_flow(spec, routed.args), track=not spec.busy_ok)
        elif routed.kind == "shell":
            self.add(UserBlock(text))
            self._run_in_background(self._shell_flow(routed))
        else:
            self.add(UserBlock(text))
            self.say(unknown_message(routed.text, self.slash, self.shell.registry), "error")

    def _run_in_background(self, flow: Any, track: bool = True) -> None:
        if track:
            self.command_running = True
        self._flow = asyncio.ensure_future(flow)

    async def _guarded(self, flow: Any, track: bool = True) -> None:
        try:
            await flow
        except asyncio.CancelledError:
            self.say("Stopped.", "warn")
        except (ClaraError, AgentError) as error:
            self.say(str(error), "error")
        except Exception as error:
            log.exception("a command failed")
            self.say(f"Unexpected error: {type(error).__name__}: {error}. See the log.", "error")
        finally:
            if track:
                self.command_running = False
            self.refresh_status()
            self.prompt_box.focus()

    async def _slash_flow(self, spec: SlashSpec, args: str) -> None:
        ctx = SlashContext(self.agent, self, self.print_line, self.print_error)
        await self._guarded(run_slash(ctx, spec, args), track=not spec.busy_ok)

    async def _shell_flow(self, routed: Routed) -> None:
        async def run() -> None:
            outcome = await run_shell_command(self.shell, routed.text, CancelToken())
            for line in outcome.lines:
                self.print_line(line)
            for error in outcome.errors:
                self.print_error(error)

        await self._guarded(run())

    async def _bang_flow(self, command: str) -> None:
        self.add(UserBlock("!" + command))
        if not command:
            self.say("usage: !command - runs it now, as you; its output goes with your next message.", "error")
            self.command_running = False
            return

        async def run() -> None:
            result = await self.agent.run_bang(command)
            body = Text(result.output or "(no output)")
            self.add(OutputBlock(body))
            status = result.note or f"exit code {result.exit_code}"
            self.say(f"{status}, {result.seconds:.1f}s. The output goes with your next message.", "dim")

        await self._guarded(run())

    async def _message_flow(self, text: str) -> None:
        try:
            prepared = await self.agent.prepare(text, list(self.chips))
        except asyncio.CancelledError:
            self.say("Stopped.", "warn")
            self.command_running = False
            return
        except (AgentError, ClaraError) as error:
            self.say(str(error), "error")
            self.command_running = False
            self.prompt_box.focus()
            return
        except Exception as error:
            log.exception("preparing a message failed")
            self.say(f"Unexpected error: {type(error).__name__}: {error}. See the log.", "error")
            self.command_running = False
            self.prompt_box.focus()
            return
        notes = list(prepared.notes) + [a.name for a in self.chips if a.name not in " ".join(prepared.notes)]
        self.chips.clear()
        self.update_chips()
        self.add(UserBlock(text, notes))
        self.transcript.anchor()
        self.command_running = False
        self.turn_running = True
        await self._turn_flow(prepared.message, prepared.attachments)

    async def _turn_flow(self, message: str, attachments: Sequence[Attachment], *, retry: bool = False) -> None:
        self.turn_running = True
        self.turn_started = time.monotonic()
        self.refresh_status()
        try:
            result = await (self.agent.retry() if retry else self.agent.send(message, attachments))
            if result.error is not None:
                self.close_blocks()
                self.say(str(result.error), "error")
        except asyncio.CancelledError:
            self.close_blocks()
            self.say("Stopped.", "warn")
        except (ClaraError, AgentError) as error:
            self.close_blocks()
            self.say(str(error), "error")
        except Exception as error:
            log.exception("the turn failed")
            self.close_blocks()
            self.say(f"Unexpected error: {type(error).__name__}: {error}. See the log.", "error")
        finally:
            self.close_blocks()
            for stale in self._pending_tools:  # a Clara tool whose end never came (the turn was stopped)
                stale.remove()
            self._pending_tools.clear()
            self.turn_running = False
            self.refresh_status()
            self.prompt_box.focus()
        self._background.append(asyncio.ensure_future(self._after_turn()))

    async def _after_turn(self) -> None:
        await self.agent.name_session()
        self.refresh_status()

    # ------------------------------------------------------------------ completion
    async def on_prompt_box_complete_requested(self, message: PromptBox.CompleteRequested) -> None:
        box = self.prompt_box
        row, column = box.cursor_location
        line = box.document.get_line(row)
        if self._complete_cancel is not None:
            self._complete_cancel.cancel()
        token = self._complete_cancel = CancelToken()
        try:
            result = await asyncio.to_thread(self.engine.complete, line, column, token, "agent")
        except Exception:
            return
        if token.cancelled or not result.candidates:
            if result.message:
                self.notify(result.message, timeout=3)
            return
        candidates = list(result.candidates)
        typed = column - result.start
        common = common_prefix([c.text for c in candidates])
        if len(candidates) == 1:
            self._insert(row, result.start, column, candidates[0].text)
            return
        if len(common) > typed:
            self._insert(row, result.start, column, common)
            column = result.start + len(common)
        self.open_popup(candidates, result.start, row, column)

    def _insert(self, row: int, start: int, column: int, text: str) -> None:
        self.close_popup()
        self.prompt_box.replace(text, (row, start), (row, column))

    def open_popup(self, candidates: list[Any], start: int, row: int, column: int) -> None:
        popup = self.query_one("#popup", OptionList)
        self._cands, self._cand_start, self._cand_row, self._cand_column = candidates, start, row, column
        popup.clear_options()
        popup.add_options([Option(Text.assemble((c.display or c.text, ""), ("   " + c.meta if c.meta else "", "dim")), id=str(i))
                           for i, c in enumerate(candidates)])
        popup.highlighted = 0
        popup.add_class("open")
        self.prompt_box.popup_open = True
        self._popup_text = self.prompt_box.text  # an edit made after this closes the popup; this one (the common part) does not

    def close_popup(self) -> None:
        popup = self.query_one("#popup", OptionList)
        popup.remove_class("open")
        self.prompt_box.popup_open = False

    def on_prompt_box_popup_key(self, message: PromptBox.PopupKey) -> None:
        popup = self.query_one("#popup", OptionList)
        key = message.key
        if key == "up":
            popup.action_cursor_up()
        elif key == "down":
            popup.action_cursor_down()
        elif key in ("tab", "enter"):
            index = popup.highlighted or 0
            if index < len(self._cands):
                self._insert(self._cand_row, self._cand_start, self._cand_column, self._cands[index].text)
        else:
            self.close_popup()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(event.option.id or 0)
        if index < len(self._cands):
            self._insert(self._cand_row, self._cand_start, self._cand_column, self._cands[index].text)
            self.prompt_box.focus()

    def on_text_area_changed(self) -> None:
        if self.prompt_box.popup_open and self.prompt_box.text != self._popup_text:
            self.close_popup()

    # ------------------------------------------------------------------ paste
    async def on_prompt_box_paste_requested(self, message: PromptBox.PasteRequested) -> None:
        desktop = self.agent.platform.desktop
        problem = ""
        try:
            image = await asyncio.to_thread(desktop.clipboard_image)
        except DesktopError as error:
            image, problem = None, str(error)
        if image:
            self.image_count += 1
            try:
                self.chips.append(pasted_image(image, self.image_count))
            except ValueError as error:
                self.say(str(error), "error")
                return
            self.update_chips()
            return
        try:
            text = await asyncio.to_thread(desktop.clipboard_read)
        except DesktopError as error:
            self.say(f"Nothing pasted: {problem or error}", "warn")
            return
        if text:
            self.prompt_box.insert(text)

    # ------------------------------------------------------------------ after the app
    def summary(self) -> str:
        s = self.agent.session
        used = s.prompt_tokens + s.completion_tokens
        title = f"'{s.title}'" if s.title else "the conversation"
        return f"Left the agent console. {title}: {used:,} tokens this session." + \
            (" /restore brings it back." if s.conversation and not s.incognito else "")


def run_agent(runtime: Runtime, shell: Shell, options: AgentOptions) -> None:
    """Close the prompt's world, run the full-screen agent in this process, come back."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        shell.err("ai: the agent console needs a terminal, and the input or the output of this program is not one")
        return
    try:
        app = AgentApp(runtime, shell, options)
    except AgentError as error:
        shell.err(f"ai: {error}")
        return
    app.run()
    shell.out(Line.of(app.summary(), "dim"))


__all__ = ["AgentApp", "AppUI", "run_agent"]
