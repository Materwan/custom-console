"""The full-screen agent app over a temp world and the fake Clara, driven by Textual's pilot."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import Static

from custom_console.core.options import AgentOptions
from custom_console.core.runtime import Runtime
from custom_console.settings import load
from custom_console.ui.agent_app import AgentApp
from tests.fake_clara.server import FakeClara
from tests.support.shelltest import Sh

SIZE = (110, 36)


def make_app(tmp_path: Path, fake: FakeClara, *, trusted: bool = True, options: AgentOptions | None = None) -> tuple[AgentApp, Sh]:
    sh = Sh(tmp_path)
    settings = load({"CONSOLE_DATA_DIR": str(tmp_path / "data"), "CLARA_URL": fake.url}, env={}, home=sh.home,
                    config_dir=tmp_path / "cfg")
    runtime = Runtime(settings, sh.shell.platform, fake.token)
    app = AgentApp(runtime, sh.shell, options or AgentOptions())
    if trusted:
        app.agent.trust_folder()
    return app, sh


async def settle(app: AgentApp, pilot: Pilot[None], until: Callable[[], bool] | None = None, timeout: float = 10.0) -> None:
    """Let the app work until it is idle (or until `until`), failing after `timeout` seconds."""
    end = time.monotonic() + timeout
    await pilot.pause(0.05)
    while time.monotonic() < end:
        await pilot.pause(0.05)
        if (until() if until is not None else not app.busy):
            await pilot.pause(0.1)
            return
    raise AssertionError("the app did not settle: " + "\n".join(transcript_lines(app)[-12:]))


def renderable_text(item: Any) -> str:
    if isinstance(item, RichMarkdown):
        return str(item.markup)
    if isinstance(item, Text):
        return item.plain
    if isinstance(item, Padding):
        return renderable_text(item.renderable)
    if isinstance(item, Group):
        return "\n".join(renderable_text(part) for part in item.renderables)
    return str(item)


def text_of(widget: Any) -> str:
    return renderable_text(widget.content) if isinstance(widget, Static) else ""


def transcript_lines(app: AgentApp) -> list[str]:
    """The text of everything in the transcript, one entry per widget with text, in order."""
    lines = []
    for widget in app.query("#transcript *"):
        text = text_of(widget)
        if text:
            lines.append(text)
    for title in app.query("Collapsible"):
        lines.append("[collapsible] " + str(title.title))  # type: ignore[attr-defined]
    return lines


def transcript_text(app: AgentApp) -> str:
    return "\n".join(transcript_lines(app))


async def type_and_send(app: AgentApp, pilot: Pilot[None], text: str) -> None:
    app.prompt_box.load_text(text)
    app.prompt_box.move_cursor(app.prompt_box.document.end)
    await pilot.press("enter")
