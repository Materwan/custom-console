"""The agent without a screen: one conversation seen from one folder, and everything a person can do with it.

The full-screen app and the slash commands call this class; it returns data or raises `AgentError` (a sentence for the
person). It builds the pieces the turn runner needs (tool context, permission engine, undo log, journal, session store),
sends messages, and wraps the Clara routes behind `/model`, `/project`, `/tools`, `/retry`, `/restore`, `/compact`, ...
Nothing here draws.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..clara import ClaraClient, ClaraError
from ..clara.types import Approval, Attachment, Conversation, Message, Models, Pack, Project, Rules
from ..platform import CancelToken
from ..platform.process import run_capture
from . import attachments as att
from .journal import Journal, prune_journals
from .ledger import Ledger, Totals
from .options import AgentOptions
from .permissions.engine import ASK, READ, REFUSE, CallInfo, Decision, PathUse, PermissionEngine, Situation
from .permissions.floor import FloorConfig
from .permissions.redact import hide_clara_tokens, scrub_environment
from .permissions.rules import Rule, RuleStore
from .permissions.trust import TrustStore
from .prompt import (
    SessionFacts,
    base_prompt,
    build_instructions,
    estimate_tokens,
    project_file_warning,
    read_project_file,
)
from .runtime import Runtime
from .session import Session, SessionStore
from .subagent import SubagentRunner, SubagentView
from .tools.base import ToolContext, ToolSpec, cut_middle, validate_args
from .tools.registry import PREFIX, ToolRegistry, ToolStatus
from .tools.run import KEEP_BYTES, JobTable
from .turn import Prompt, TurnResult, TurnRunner, TurnUI
from .undo import UndoLog, UndoReport
from .vfs.guard import OverwriteGuard
from .vfs.ops import Vfs

BANG_TIMEOUT = 600.0
BANG_OUTPUT_LIMIT = 20_000
SHOWN_VERSIONS = 10


class AgentError(Exception):
    """Something the person asked cannot be done; the message says why and what to do instead."""


class AgentUI(TurnUI, SubagentView, Protocol):
    """Everything a screen provides to the agent: the turn's prompts and events, and the sub-agent block."""


@dataclass(slots=True)
class BangResult:
    command: str
    output: str
    exit_code: int | None
    seconds: float
    note: str = ""  # "stopped", "timed out"


@dataclass(frozen=True, slots=True)
class ContextReport:
    model: str
    window: int
    used_tokens: int
    percent: float
    instructions_tokens: int
    project_tokens: int
    tools_tokens: int
    tools_offered: int


@dataclass(frozen=True, slots=True)
class PackState:
    pack: Pack
    on: bool


@dataclass(slots=True)
class Restored:
    session: Session
    messages: tuple[Message, ...]


@dataclass(slots=True)
class VersionInfo:
    message: Message
    ids: tuple[int, ...]
    active: int  # 1-based
    answer: str = ""


@dataclass(slots=True)
class RuleReport:
    local: list[Rule] = field(default_factory=list)
    clara: Rules | None = None
    clara_error: str = ""


class Agent:
    def __init__(self, runtime: Runtime, vfs: Vfs, env: Mapping[str, str], options: AgentOptions, ui: AgentUI, *,
                 client: ClaraClient | None = None, trust: TrustStore | None = None):
        self.runtime, self.env, self.ui = runtime, env, ui
        self.platform, self.layout, self.settings = runtime.platform, runtime.settings.data, runtime.settings
        self.vfs = vfs.fork()
        self.folder = self.vfs.here()
        if not self.folder.on_disk:
            raise AgentError("The agent works in a folder on a disk. Go to one with `cd`, then run `ai agent`.")
        info = self.vfs.stat(self.folder)
        if info is None or not info.is_dir:
            raise AgentError(f"{self.folder.virtual} is not a folder.")
        self.real_folder = self.folder.real()
        ci = self.platform.paths.case_insensitive
        self.client = client or runtime.new_client()
        self._owns_client = client is None
        self.trust = trust or TrustStore(self.layout.trust, ci)
        self.rules = RuleStore(self.layout.rules)
        floor = FloorConfig(tuple(str(p) for p in self.platform.paths.credential_dirs(self.platform.paths.home())),
                            (str(self.layout.root), str(self.settings.token_file), str(self.settings.config_dir)), ci)
        self.engine = PermissionEngine(self.platform, floor, self.rules)
        self.registry = ToolRegistry()
        self.undo = UndoLog(self.layout.undo)
        self.ledger = Ledger(self.layout.usage)
        self.store = SessionStore(self.layout.sessions, self.real_folder, case_insensitive=ci)
        self.jobs = JobTable()
        self.ctx = ToolContext(
            vfs=self.vfs, platform=self.platform, folder=self.folder, trusted=self.trust.is_trusted(self.real_folder),
            cancel=self.vfs.cancel, guard=OverwriteGuard(ci), floor=floor, env=scrub_environment(os.environ),
            tool_timeout=self.settings.tool_timeout, result_limit=self.settings.result_limit, undo=self.undo,
            jobs=self.jobs, git=shutil.which("git"))
        self._models: Models | None = None
        self._bang_cancel: CancelToken | None = None
        self.session = Session(folder=self.real_folder, incognito=options.incognito, local_only=options.local_only,
                               model=options.model)
        self._bind_session()

    # ------------------------------------------------------------------ the session
    def _bind_session(self) -> None:
        """Make every part that follows the session follow the current one (after /clear and /restore)."""
        session = self.session
        self.journal = Journal(self.layout.journal, session.id)
        self.runner = TurnRunner(client=self.client, session=session, registry=self.registry, engine=self.engine, ctx=self.ctx,
                                 ui=self.ui, journal=self.journal, ledger=self.ledger, instructions=self.instructions,
                                 store=self.store)
        self.ctx.todos = session.todos
        self.ctx.ask = self.ui.ask_user
        self.ctx.subagent = SubagentRunner(client=self.client, parent=session, registry=self.registry, engine=self.engine,
                                           ctx=self.ctx, ui=self.ui, view=self.ui, journal=self.journal, ledger=self.ledger,
                                           parent_runner=lambda: self.runner)
        self.ctx.guard = OverwriteGuard(self.platform.paths.case_insensitive)
        prune_journals(self.layout.journal)

    def new_session(self, *, incognito: bool = False, local_only: bool = False) -> Session:
        """`/clear`: a new conversation. The model, the project and the tool switches carry over; the rest does not."""
        old = self.session
        self.session = Session(folder=self.real_folder, incognito=incognito, local_only=local_only, model=old.model,
                               project=old.project, packs=list(old.packs) if old.packs is not None else None,
                               disabled_tools=set(old.disabled_tools))
        self.session.plan_mode = old.plan_mode
        self._bind_session()
        return self.session

    async def aclose(self) -> None:
        self.jobs.stop_all()
        if self._owns_client:
            await self.client.aclose()

    @property
    def trusted(self) -> bool:
        return self.ctx.trusted

    def trust_folder(self) -> None:
        self.trust.trust(self.real_folder)
        self.ctx.trusted = True

    # ------------------------------------------------------------------ instructions and context
    def facts(self) -> SessionFacts:
        s = self.session
        return SessionFacts(self.platform.name, self.folder.virtual, self.real_folder, self.trusted, s.plan_mode, s.incognito,
                            s.local_only, tuple(sorted(s.disabled_tools)))

    def instructions(self) -> str:
        """Read fresh for each turn: `AGENT.md` may have been edited, and /plan, /tools change the facts."""
        return build_instructions(self.facts(), read_project_file(self.vfs, self.folder, self.trusted))

    def project_warning(self) -> str:
        return project_file_warning(self.vfs, self.folder, self.trusted)

    def offered(self) -> list[ToolSpec]:
        return self.registry.offered(self.ctx, disabled=frozenset(self.session.disabled_tools), plan_mode=self.session.plan_mode)

    def context_report(self) -> ContextReport:
        s = self.session
        specs = self.offered()
        schemas = json.dumps([spec.schema() for spec in specs])
        project = read_project_file(self.vfs, self.folder, self.trusted)
        return ContextReport(s.model or self._models_default(), s.context_window, s.context_tokens, s.context_percent,
                             estimate_tokens(base_prompt() + self.facts().render()), estimate_tokens(project),
                             estimate_tokens(schemas), len(specs))

    def _models_default(self) -> str:
        return self._models.default if self._models is not None else ""

    # ------------------------------------------------------------------ sending
    def situation(self) -> Situation:
        return Situation(self.folder, self.trusted, False, self.session.plan_mode)

    async def prepare(self, text: str, pasted: Sequence[Attachment] = ()) -> att.Prepared:
        """Resolve `@file` mentions, ask about the ones that need it, read them. Raises AgentError when the message must not go."""
        mentioned, problems = att.resolve_mentions(text, self.vfs)
        if problems:
            raise AgentError("Not sent. " + "; ".join(problems))
        taken = {a.name for a in pasted}
        loaded: list[att.Loaded] = []
        for item in mentioned:
            allowed, why = await self._may_attach(item)
            if not allowed:
                loaded.append(att.Loaded(note=f"@{item.typed}: not attached ({why})"))
                continue
            loaded.append(await asyncio.to_thread(att.load, self.vfs, item, taken))
        return att.compose(text, loaded, pasted)

    async def _may_attach(self, item: att.Mentioned) -> tuple[bool, str]:
        info = CallInfo("mention", READ, paths=(PathUse(item.resolved.virtual, "read"),),
                        summary=f"Send {item.resolved.virtual} with your message")
        decision = self.engine.decide(info, self.vfs, self.situation())
        if decision.effect == REFUSE:
            return False, decision.reason
        if decision.effect != ASK:
            return True, ""
        prompt = Prompt("local", "mention", info.summary, decision.reason, "", decision.paths, "", decision.can_remember,
                        decision.why_not_remembered, None, floor=decision.floor)
        answer = await self.ui.confirm(prompt)
        note = self.engine.record(decision, answer)
        if note:
            self.ui.note(note)
        return (not answer.startswith("refuse")), "you refused"

    async def _ensure_conversation(self) -> None:
        """Clara makes a conversation on the first message, but cannot take tool packs there: create it first when they are set."""
        s = self.session
        if s.conversation is not None or s.packs is None:
            return
        made = await self.client.create_conversation(project=s.project, model=s.model or "", incognito=s.incognito,
                                                     local_only=s.local_only, packs=list(s.packs))
        s.conversation = made.id

    async def send(self, text: str, attachments: Sequence[Attachment] = (), *, fork_of: int | None = None) -> TurnResult:
        await self._ensure_conversation()
        self.undo.begin_turn(uuid.uuid4().hex[:12])
        tainted = bool(self.session.pending_notes)  # the output of a command may be anything: treat it as outside content
        return await self.runner.run(text, attachments=attachments, fork_of=fork_of, tainted=tainted)

    def cancel(self) -> None:
        self.runner.cancel()
        if self._bang_cancel is not None:
            self._bang_cancel.cancel()

    async def name_session(self) -> str:
        """Ask Clara for a title after the first answer. Never raises: a missing title is not worth a message."""
        s = self.session
        if s.title or s.conversation is None or s.incognito:
            return s.title
        try:
            s.title = await asyncio.wait_for(self.client.title(s.conversation), 30)
            self.store.save(s)
        except (ClaraError, TimeoutError, OSError):
            pass
        return s.title

    # ------------------------------------------------------------------ `!command`
    async def run_bang(self, command: str) -> BangResult:
        """Run a command now, as the person, in the working folder. Its output goes with the next message."""
        token = CancelToken()
        self._bang_cancel = token
        try:
            done = await asyncio.to_thread(run_capture, command, shell=True, cwd=self.real_folder, env=dict(self.env),
                                           timeout=BANG_TIMEOUT, cancel=token, keep=KEEP_BYTES)
        finally:
            self._bang_cancel = None
        output = hide_clara_tokens(cut_middle(done.output.rstrip("\n"), BANG_OUTPUT_LIMIT))
        note = "stopped" if done.cancelled else "timed out" if done.timed_out else ""
        result = BangResult(command, output, done.returncode, done.seconds, note)
        status = note or f"exit code {done.returncode}"
        fence = att.fence(output)
        self.session.pending_notes.append(
            f"[The person ran `{command}` in {self.folder.virtual} ({status}). Output:]\n{fence}\n{output or '(no output)'}\n{fence}")
        return result

    # ------------------------------------------------------------------ models, projects, packs
    async def models(self) -> Models:
        if self._models is None:
            self._models = await self.client.models()
        return self._models

    async def set_model(self, name: str) -> str:
        """`/model NAME`: by reference, or by name when only one model has it. `default` goes back to Clara's choice."""
        if name.lower() in ("default", "auto"):
            self.session.model = None
            return ""
        models = (await self.models()).models
        exact = [m for m in models if m.ref == name]
        found = exact or [m for m in models if m.name == name] or [m for m in models if m.ref.startswith(name)]
        if len(found) != 1:
            hint = ", ".join(m.ref for m in models[:12])
            raise AgentError(f"{'No model' if not found else 'More than one model'} matches {name!r}. Models: {hint}")
        if self.session.local_only and not found[0].local:
            raise AgentError(f"{found[0].ref} runs outside this network and this conversation is local only.")
        self.session.model = found[0].ref
        return found[0].ref

    async def projects(self) -> tuple[Project, ...]:
        return await self.client.projects()

    async def set_project(self, choice: str) -> Project | None:
        s = self.session
        if choice.lower() in ("none", "0", "-"):
            s.project = None
            if s.conversation:
                await self.client.change_conversation(s.conversation, project=0)
            return None
        projects = await self.projects()
        found = [p for p in projects if str(p.id) == choice or p.name.lower() == choice.lower()]
        if len(found) != 1:
            raise AgentError(f"No project {choice!r}. Projects: " + (", ".join(f"{p.id} {p.name}" for p in projects) or "none"))
        s.project = found[0].id
        if s.conversation:
            await self.client.change_conversation(s.conversation, project=found[0].id)
        return found[0]

    async def packs(self) -> list[PackState]:
        s = self.session
        packs = await self.client.packs("chat")
        if s.packs is None and s.conversation:
            with contextlib.suppress(ClaraError):
                s.packs = list(Conversation.from_json(await self.client.conversation(s.conversation)).packs) or None
        enabled = set(s.packs) if s.packs is not None else {p.id for p in packs if p.default}
        return [PackState(p, p.id in enabled) for p in packs]

    async def reset_packs(self) -> None:
        """Back to the server's defaults."""
        packs = await self.client.packs("chat")
        self.session.packs = None
        if self.session.conversation:
            await self.client.change_conversation(self.session.conversation, packs=sorted(p.id for p in packs if p.default and p.available))

    async def set_pack(self, pack_id: str, on: bool) -> None:
        states = await self.packs()
        found = [p for p in states if p.pack.id == pack_id]
        if not found:
            raise AgentError(f"No tool pack {pack_id!r}. Packs: {', '.join(p.pack.id for p in states)}")
        if on and not found[0].pack.available:
            raise AgentError(f"{pack_id} is not available: {found[0].pack.reason or 'Clara says no'}.")
        wanted = {p.pack.id for p in states if p.on} - {pack_id}
        if on:
            wanted.add(pack_id)
        self.session.packs = sorted(wanted)
        if self.session.conversation:
            await self.client.change_conversation(self.session.conversation, packs=self.session.packs)

    # ------------------------------------------------------------------ local tools
    def tool_states(self) -> list[ToolStatus]:
        return self.registry.status(self.ctx, disabled=frozenset(self.session.disabled_tools), plan_mode=self.session.plan_mode)

    def resolve_tool_name(self, name: str) -> str:
        full = name if name.startswith(PREFIX) else PREFIX + name
        if self.registry.get(full) is None:
            raise AgentError(f"No local tool {name!r}. Tools: {', '.join(n.removeprefix(PREFIX) for n in self.registry.names)}")
        return full

    def set_tool(self, name: str, on: bool) -> None:
        full = self.resolve_tool_name(name)
        (self.session.disabled_tools.discard if on else self.session.disabled_tools.add)(full)

    def reset_tools(self) -> None:
        self.session.disabled_tools.clear()

    def set_plan_mode(self, on: bool) -> None:
        self.session.plan_mode = on

    # ------------------------------------------------------------------ versions and retry
    async def _last_question(self) -> Message:
        cid = self.session.conversation
        if cid is None:
            raise AgentError("There is no conversation yet: send a message first.")
        asked = [m for m in await self.client.messages(cid) if m.role == "user"]
        if not asked:
            raise AgentError("There is no message to answer again.")
        return asked[-1]

    async def retry(self) -> TurnResult:
        """Answer the last question again as a new version. Clara refuses local tools with `fork_of`, so this answer has none."""
        question = await self._last_question()
        return await self.send(question.content, (), fork_of=question.id)

    async def versions(self) -> VersionInfo:
        question = await self._last_question()
        ids = question.versions or (question.id,)
        active = ids.index(question.id) + 1 if question.id in ids else max(question.version, 1)
        return VersionInfo(question, ids, active)

    async def switch_version(self, number: int) -> VersionInfo:
        info = await self.versions()
        if not 1 <= number <= len(info.ids):
            raise AgentError(f"There {'is' if len(info.ids) == 1 else 'are'} {len(info.ids)} version"
                             f"{'' if len(info.ids) == 1 else 's'}: choose 1 to {len(info.ids)}.")
        cid = self.session.conversation
        assert cid is not None
        await self.client.switch_version(cid, info.ids[number - 1])
        messages = await self.client.messages(cid)
        answers = [m for m in messages if m.role == "assistant"]
        self.session.last_message_id = info.ids[number - 1]
        return VersionInfo(info.message, info.ids, number, answers[-1].content if answers else "")

    # ------------------------------------------------------------------ restore, compact, undo
    def restorable(self) -> list[Session]:
        return self.store.list()

    async def restore(self, number: int) -> Restored:
        saved = self.restorable()
        if not 1 <= number <= len(saved):
            raise AgentError(f"Choose 1 to {len(saved)}." if saved else "No saved session for this folder.")
        chosen = saved[number - 1]
        assert chosen.conversation is not None
        try:
            messages = await self.client.messages(chosen.conversation)
        except ClaraError as error:
            if error.code == "not_found":
                self.store.forget(chosen)
                raise AgentError("That conversation is gone from Clara; it was removed from the list.") from None
            raise
        self.session = chosen
        self._bind_session()
        return Restored(chosen, messages)

    async def compact(self, focus: str = "") -> tuple[float, float]:
        cid = self.session.conversation
        if cid is None:
            raise AgentError("There is nothing to compact yet.")
        return await self.client.compact(cid, focus)

    async def undo_last(self) -> UndoReport:
        return await asyncio.to_thread(self.undo.undo_last)

    # ------------------------------------------------------------------ usage, approvals, rules, records
    def usage(self, window: str) -> Totals:
        try:
            return self.ledger.window(window, self.session.id)
        except ValueError as error:
            raise AgentError(str(error)) from None

    async def approvals(self) -> tuple[Approval, ...]:
        return await self.client.approvals("pending")

    async def decide_approval(self, approval_id: int, answer: str) -> Approval:
        approval = await self.client.decide(approval_id, answer)
        self.journal.approval(approval_id, approval.tool, answer, "console (/approvals)")
        return approval

    async def rule_report(self) -> RuleReport:
        report = RuleReport(local=self.rules.list())
        try:
            report.clara = await self.client.rules()
        except ClaraError as error:
            report.clara_error = str(error)
        return report

    def decide_local(self, tool: str, arguments: dict[str, Any], *, tainted: bool = False) -> Decision:
        """What the permission engine would do with this call, without running it."""
        spec = self.registry.get(self.resolve_tool_name(tool))
        assert spec is not None
        problems = validate_args(spec.parameters, arguments)
        if problems:
            raise AgentError("; ".join(problems))
        info = spec.inspect(self.ctx, arguments)
        situation = Situation(self.folder, self.trusted, tainted, self.session.plan_mode)
        return self.engine.decide(info, self.vfs, situation)

    async def test_clara_rule(self, tool: str, arguments: dict[str, Any], *, tainted: bool = False) -> dict[str, Any]:
        return await self.client.test_rule(tool, arguments, tainted=tainted, local_only=self.session.local_only)

    def journal_records(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.journal.read(limit)

    async def transcript(self) -> tuple[Message, ...]:
        cid = self.session.conversation
        if cid is None:
            raise AgentError("There is no conversation yet.")
        return await self.client.messages(cid)

    async def conversation(self) -> Conversation | None:
        cid = self.session.conversation
        return None if cid is None else Conversation.from_json(await self.client.conversation(cid))
