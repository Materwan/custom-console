"""An `Agent` in a temp folder against the fake Clara, with a fake screen. Nothing here can reach the real home."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from custom_console.clara import ClaraClient
from custom_console.clara.types import Message
from custom_console.commands.base import Line, Registry
from custom_console.commands.shell import build_registry
from custom_console.commands.slash import build_slash_registry
from custom_console.commands.slash.base import SlashContext, SlashRegistry
from custom_console.commands.slash.router import Routed, route, run_slash, unknown_message
from custom_console.core.agent import Agent
from custom_console.core.options import AgentOptions
from custom_console.core.runtime import Runtime
from custom_console.core.vfs.ops import Vfs
from custom_console.settings import load
from tests.fake_clara.server import FakeClara
from tests.support.harness import FakeDesktop, FakeUI, sandbox_platform, sandbox_resolver


class AgentFakeUI(FakeUI):
    """The turn's fake UI plus the sub-agent block."""

    def __init__(self, answers: Any = None) -> None:
        super().__init__(answers)
        self.sub: list[tuple[str, Any]] = []

    def sub_started(self, label: str, prompt: str, round_limit: int) -> int:
        self.sub.append(("start", label))
        return len([s for s in self.sub if s[0] == "start"])

    def sub_event(self, number: int, event: Any) -> None:
        self.sub.append(("event", event))

    def sub_tool(self, number: int, call: Any, ok: bool, result: str, seconds: float, preview: str) -> None:
        self.sub.append(("tool", call.name))

    def sub_waiting(self, number: int, waiting: bool) -> None:
        self.sub.append(("waiting", waiting))

    def sub_finished(self, number: int, ok: bool, summary: str) -> None:
        self.sub.append(("end", (ok, summary)))


class FakeScreen:
    """Records what the slash commands ask of the screen."""

    def __init__(self) -> None:
        self.cleared = 0
        self.answers: list[str] = []
        self.replayed: list[Sequence[Message]] = []
        self.turns: list[tuple[str, str, bool]] = []
        self.views: list[tuple[str, list[Line]]] = []
        self.refreshed = 0
        self.exited = False
        self.picks: list[tuple[str, list[tuple[str, str]], str]] = []
        self.choose: str | None = None  # what the next pick() returns (None: the user cancels)

    def clear_transcript(self) -> None:
        self.cleared += 1

    def show_answer(self, text: str) -> None:
        self.answers.append(text)

    def replay(self, messages: Sequence[Message]) -> None:
        self.replayed.append(messages)

    def start_turn(self, text: str, *, label: str = "", retry: bool = False) -> None:
        self.turns.append((text, label, retry))

    def view(self, title: str, lines: Sequence[Line]) -> None:
        self.views.append((title, list(lines)))

    async def pick(self, title: str, rows: Sequence[tuple[str, str]], current: str = "") -> str | None:
        self.picks.append((title, list(rows), current))
        return self.choose

    def status_changed(self) -> None:
        self.refreshed += 1

    def exit(self) -> None:
        self.exited = True


@dataclass
class AgentWorld:
    agent: Agent
    fake: FakeClara
    ui: AgentFakeUI
    screen: FakeScreen
    root: Path
    home: Path
    work: Path
    data: Path
    desktop: FakeDesktop
    slash: SlashRegistry = field(default_factory=build_slash_registry)
    shell_registry: Registry = field(default_factory=build_registry)
    out: list[Line] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def context(self) -> SlashContext:
        return SlashContext(self.agent, self.screen, self.out.append, self.errors.append)

    async def command(self, text: str) -> str:
        """Type a slash command; returns what it printed (errors included), one string."""
        routed: Routed = route(text, self.slash, self.shell_registry)
        assert routed.kind == "slash", routed
        spec = self.slash.get(routed.name)
        assert spec is not None
        before_out, before_err = len(self.out), len(self.errors)
        await run_slash(self.context(), spec, routed.args)
        return "\n".join([*(line.plain for line in self.out[before_out:]), *self.errors[before_err:]])

    def unknown(self, name: str) -> str:
        return unknown_message(name, self.slash, self.shell_registry)


def make_agent_world(tmp_path: Path, fake: FakeClara, client: ClaraClient, *, trusted: bool = True,
                     options: AgentOptions | None = None, answers: Any = None, cwd: str | None = None,
                     remarkable: Any = None) -> AgentWorld:
    root = tmp_path
    home = root / "home"
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    desktop = FakeDesktop()
    platform = sandbox_platform(home, desktop)
    resolver = sandbox_resolver(root, home, platform, remarkable=remarkable)
    vfs = Vfs(resolver, cwd=cwd if cwd is not None else str(work))
    settings = load({"CONSOLE_DATA_DIR": str(data)}, env={}, home=home, config_dir=root / "cfg")
    runtime = Runtime(settings, platform, fake.token)
    ui = AgentFakeUI(answers)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    agent = Agent(runtime, vfs, env, options or AgentOptions(), ui, client=client)
    if trusted:
        agent.trust_folder()
    return AgentWorld(agent, fake, ui, FakeScreen(), root, home, work, data, desktop)
