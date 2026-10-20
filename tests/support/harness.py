"""A whole agent world in a temp folder: tool context, permission engine, session, journal, ledger, and a fake UI."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from custom_console.clara.events import ToolCall
from custom_console.core.journal import Journal
from custom_console.core.ledger import Ledger
from custom_console.core.permissions.engine import PermissionEngine
from custom_console.core.permissions.floor import FloorConfig
from custom_console.core.permissions.redact import scrub_environment
from custom_console.core.permissions.rules import RuleStore
from custom_console.core.session import Session, SessionStore
from custom_console.core.tools.base import Question, ToolContext
from custom_console.core.tools.registry import ToolRegistry
from custom_console.core.tools.run import JobTable
from custom_console.core.turn import Prompt, TurnRunner
from custom_console.core.undo import UndoLog
from custom_console.core.vfs.guard import OverwriteGuard
from custom_console.core.vfs.local import LocalBackend
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.resolver import Resolver
from custom_console.platform import AppMatch, DesktopError, Platform, for_system
from tests.support.guard import SandboxViolation


class FakeDesktop:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.started: list[AppMatch] = []
        self.clipboard = "clip text"
        self.image: bytes | None = None
        self.apps = {"editor": AppMatch("Editor", "/usr/bin/editor", 1)}
        self.fail: str | None = None
        self.restarted: list[str] | None = None

    def _check(self) -> None:
        if self.fail:
            raise DesktopError(self.fail)

    def open_path(self, path: Path) -> None:
        self._check()
        self.opened.append(path)

    def find_app(self, name: str, level: int = 4) -> AppMatch | None:
        return self.apps.get(name.lower())

    def start_app(self, match: AppMatch) -> None:
        self._check()
        self.started.append(match)

    def start_file(self, path: Path) -> None:
        self._check()

    def clipboard_read(self) -> str:
        self._check()
        return self.clipboard

    def clipboard_write(self, text: str) -> None:
        self._check()
        self.clipboard = text

    def clipboard_image(self) -> bytes | None:
        self._check()
        return self.image

    def restart_console(self, argv: list[str]) -> None:
        self._check()
        self.restarted = argv


class SandboxedLocalBackend(LocalBackend):
    """The local disk, except that it refuses (before touching anything) to write, remove or rename outside `root`.
    Reading anywhere is allowed: reading destroys nothing."""

    def __init__(self, root: Path):
        self.root = os.path.realpath(root)

    def _inside(self, real: str) -> bool:
        return real == self.root or real.startswith(self.root + os.sep)

    def _through(self, inner: str) -> None:
        """For operations that follow a link (write, touch, mkdir): the real destination must be inside."""
        real = os.path.realpath(inner)
        if not self._inside(real):
            raise SandboxViolation(f"REFUSED: {inner} (really {real}) is outside the sandbox {self.root}")

    def _leaf(self, inner: str) -> None:
        """For operations that act on the entry itself, never on what a link points to (remove, rmdir, rename)."""
        parent, name = os.path.split(os.path.abspath(inner))
        real = os.path.join(os.path.realpath(parent), name)
        if not self._inside(real):
            raise SandboxViolation(f"REFUSED: {inner} (really {real}) is outside the sandbox {self.root}")

    def write(self, inner: str, data: bytes, cancel: Any = None) -> None:
        self._through(inner)
        super().write(inner, data, cancel)

    def mkdir(self, inner: str, parents: bool = False) -> None:
        self._through(inner)
        super().mkdir(inner, parents)

    def touch(self, inner: str) -> None:
        self._through(inner)
        super().touch(inner)

    def rename(self, source: str, destination: str) -> None:
        self._leaf(source)
        self._leaf(destination)
        super().rename(source, destination)

    def remove(self, inner: str) -> None:
        self._leaf(inner)
        super().remove(inner)

    def rmdir(self, inner: str) -> None:
        self._leaf(inner)
        super().rmdir(inner)


def sandbox_platform(home: Path, desktop: Any = None) -> Platform:
    """The Linux platform with a home inside the temp tree (and, on request, a fake desktop)."""
    base = for_system("linux")
    return replace(base, desktop=desktop if desktop is not None else base.desktop, paths=SandboxPaths(base.paths, home))


def sandbox_resolver(root: Path, home: Path, platform: Platform | None = None, **kwargs: Any) -> Resolver:
    """A resolver whose local disk cannot be changed outside `root`."""
    return Resolver(platform or sandbox_platform(home), home=home, local=SandboxedLocalBackend(root), **kwargs)


class SandboxPaths:
    """The real path rules, except that `home()` is a folder inside the temp tree: no test may ever mean the real home."""

    def __init__(self, inner: Any, home: Path):
        self._inner = inner
        self._home = home

    def home(self) -> Path:
        return self._home

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class FakeUI:
    """Records everything; answers prompts from `answers` (a list used in order, or a function of the prompt)."""

    def __init__(self, answers: list[str] | Callable[[Prompt], str] | None = None):
        self.answers = answers if answers is not None else []
        self.events: list[Any] = []
        self.prompts: list[Prompt] = []
        self.notes: list[str] = []
        self.finished: list[tuple[str, bool, str]] = []
        self.closed: list[int] = []
        self.questions: list[Question] = []
        self.reply = "an answer"
        self.hold: Any = None  # an asyncio.Event a test can use to keep a prompt open

    def show(self, event: Any) -> None:
        self.events.append(event)

    async def confirm(self, prompt: Prompt) -> str:
        self.prompts.append(prompt)
        if self.hold is not None:
            await self.hold.wait()
        if callable(self.answers):
            return self.answers(prompt)
        return self.answers.pop(0) if self.answers else "allow_once"

    async def ask_user(self, question: Question) -> str:
        self.questions.append(question)
        return self.reply

    def approval_closed(self, approval_id: int, note: str) -> None:
        self.closed.append(approval_id)

    def tool_finished(self, call: ToolCall, ok: bool, result: str, seconds: float, preview: str) -> None:
        self.finished.append((call.name, ok, result))

    def note(self, text: str) -> None:
        self.notes.append(text)


@dataclass
class World:
    root: Path
    home: Path
    work: Path
    data: Path
    platform: Platform
    desktop: FakeDesktop
    vfs: Vfs
    ctx: ToolContext
    engine: PermissionEngine
    rules: RuleStore
    registry: ToolRegistry
    session: Session
    journal: Journal
    ledger: Ledger
    undo: UndoLog
    store: SessionStore
    ui: FakeUI = field(default_factory=FakeUI)

    def runner(self, client: Any, **kwargs: Any) -> TurnRunner:
        return TurnRunner(client=client, session=self.session, registry=self.registry, engine=self.engine, ctx=self.ctx,
                          ui=self.ui, journal=self.journal, ledger=self.ledger, instructions=lambda: "BASE PROMPT",
                          store=self.store, **kwargs)

    async def call(self, tool: str, /, **arguments: Any) -> str:
        """Run one tool call through the whole path: validation, permission, prompt, handler, shaping."""
        runner = self.runner(None)
        runner._begin()
        runner._offered = set(self.registry.names)
        return await runner.execute_call(ToolCall(f"call_{tool}", tool, arguments), 100.0)

    async def run(self, tool: str, /, **arguments: Any) -> str:
        """Run a handler directly (no permission), for tool behaviour tests."""
        from custom_console.core.tools.base import call_handler
        spec = self.registry.get(tool)
        assert spec is not None
        return await call_handler(spec, self.ctx, arguments)


def make_world(tmp_path: Path, *, trusted: bool = True, answers: list[str] | Callable[[Prompt], str] | None = None) -> World:
    root = tmp_path
    home = root / "home"
    work = home / "work"
    work.mkdir(parents=True)
    data = root / "data"
    data.mkdir()
    token = home / ".config" / "clara" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("clara_a_b\n")
    desktop = FakeDesktop()
    platform = sandbox_platform(home, desktop)
    resolver = sandbox_resolver(root, home, platform)
    vfs = Vfs(resolver, cwd=str(work))
    floor = FloorConfig(tuple(str(p) for p in platform.paths.credential_dirs(home)), (str(data), str(token)), False)
    rules = RuleStore(data / "rules.json")
    engine = PermissionEngine(platform, floor, rules)
    undo = UndoLog(data / "undo")
    ctx = ToolContext(vfs=vfs, platform=platform, folder=vfs.here(), trusted=trusted, cancel=vfs.cancel,
                      guard=OverwriteGuard(), floor=floor, env=scrub_environment(os.environ), tool_timeout=20.0,
                      result_limit=30_000, undo=undo, jobs=JobTable(), git=shutil.which("git"))
    session = Session(folder=str(work))
    return World(root, home, work, data, platform, desktop, vfs, ctx, engine, rules, ToolRegistry(), session,
                 Journal(data / "journal", session.id), Ledger(data / "usage.jsonl"), undo,
                 SessionStore(data / "sessions", str(work)), FakeUI(answers))
