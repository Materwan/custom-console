"""The agent screen against a real Clara with a real token. Run by hand, never in CI:

    uv run pytest -m live tests/live/test_live_agent.py -v

It makes short real model calls in an incognito conversation (which it deletes at the end) and only reads files in a temp
folder. It never asks the model to use a tool that changes anything.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from custom_console.clara import DEFAULT_URL, ClaraError, read_token
from custom_console.core.options import AgentOptions
from custom_console.core.runtime import Runtime
from custom_console.settings import load
from custom_console.ui.agent_app import AgentApp
from custom_console.ui.widgets.blocks import AnswerBlock, ApprovalBlock
from tests.conftest import REAL_HOME
from tests.support.appworld import SIZE, settle, transcript_text, type_and_send
from tests.support.shelltest import Sh

pytestmark = pytest.mark.live


async def test_a_real_question_with_a_real_local_tool_round(tmp_path):
    token_file = Path(os.environ.get("CLARA_TOKEN_FILE") or REAL_HOME / ".config" / "clara" / "token")
    url = os.environ.get("CLARA_URL", DEFAULT_URL)
    sh = Sh(tmp_path)
    sh.write("hello-live.txt", "a file for the live test\n")
    settings = load({"CONSOLE_DATA_DIR": str(tmp_path / "data"), "CLARA_URL": url}, env={}, home=sh.home, config_dir=tmp_path / "cfg")
    app = AgentApp(Runtime(settings, sh.shell.platform, read_token(token_file)), sh.shell, AgentOptions(incognito=True))
    app.agent.trust_folder()
    async with app.run_test(size=SIZE) as pilot:
        try:
            await type_and_send(app, pilot, "Call the local_list tool once, with no arguments, then reply with only the name of the "
                                            "file it shows. Use no other tool.")
            await settle(app, pilot, timeout=180)
            text = transcript_text(app)
            assert "hello-live.txt" in text and not app.query(ApprovalBlock), text[-800:]
            assert "list" in " ".join(str(c.title) for c in app.query("ToolBlock")), "no local tool line was drawn"
            answers = [a for a in app.query(AnswerBlock) if a.text.strip()]
            assert answers and "hello-live.txt" in answers[-1].text
            assert app.agent.session.conversation and app.agent.session.prompt_tokens > 0

            await type_and_send(app, pilot, "/usage session")
            await settle(app, pilot)
            await type_and_send(app, pilot, "/model")
            await settle(app, pilot)
            await type_and_send(app, pilot, "/rules")
            await settle(app, pilot)
            shown = transcript_text(app)
            assert "tokens counted" in shown.lower() or "Tokens counted" in shown
            assert "Models" in shown and "Local rules" in shown and "Clara's rules" in shown
            journal = app.agent.journal_records()
            assert any(r["kind"] == "tool" and r["name"] == "local_list" and r["step"] == "allowlist" for r in journal)
        finally:
            conversation = app.agent.session.conversation
            if conversation:
                with contextlib.suppress(ClaraError):
                    await app.agent.client.delete_conversation(conversation)


async def test_a_real_permission_prompt_for_a_local_write_and_then_undo(tmp_path):
    """The console's own prompt with a real model: it asks, one key answers, the file appears, /undo takes it away.
    Only `local_write` in a temp folder is asked for; Clara's own workspace tools are never named."""
    token_file = Path(os.environ.get("CLARA_TOKEN_FILE") or REAL_HOME / ".config" / "clara" / "token")
    sh = Sh(tmp_path)
    settings = load({"CONSOLE_DATA_DIR": str(tmp_path / "data"), "CLARA_URL": os.environ.get("CLARA_URL", DEFAULT_URL)}, env={},
                    home=sh.home, config_dir=tmp_path / "cfg")
    app = AgentApp(Runtime(settings, sh.shell.platform, read_token(token_file)), sh.shell, AgentOptions(incognito=True))
    app.agent.trust_folder()
    async with app.run_test(size=SIZE) as pilot:
        try:
            await type_and_send(app, pilot, "Use the local_write tool to create the file made.txt with the content live. "
                                            "Use no other tool, then say done.")
            await settle(app, pilot, until=lambda: bool(app.query(ApprovalBlock)), timeout=120)
            shown = "\n".join(str(getattr(w, "content", "")) for w in app.query(ApprovalBlock).first().query("Static"))
            assert "made.txt" in shown
            assert not (sh.work / "made.txt").exists()      # nothing happened before the answer
            await pilot.press("o")
            await settle(app, pilot, timeout=120)
            assert (sh.work / "made.txt").read_text().strip() == "live"
            await type_and_send(app, pilot, "/undo")
            await settle(app, pilot)
            assert not (sh.work / "made.txt").exists() and "deleted" in transcript_text(app)
        finally:
            conversation = app.agent.session.conversation
            if conversation:
                with contextlib.suppress(ClaraError):
                    await app.agent.client.delete_conversation(conversation)
