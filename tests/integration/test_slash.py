"""The agent's `/commands`: what each prints, what it asks of the screen, and how a typed line is understood."""

from __future__ import annotations

import sys

import pytest

from custom_console.commands.shell import new_shell
from custom_console.commands.slash.router import BLOCKED_IN_AGENT, route
from custom_console.commands.slash.shellbridge import run_shell_command
from custom_console.core.options import AgentOptions
from custom_console.core.prompt import INIT_REQUEST
from custom_console.platform import CancelToken
from tests.fake_clara.server import WaitResults
from tests.support.agent_world import make_agent_world

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
CID = "c0123456789abcdef"
PLAN_COMMANDS = {"model", "tools", "clear", "project", "retry", "version", "restore", "undo", "compact", "context", "usage",
                 "approvals", "rules", "todo", "plan", "init", "journal", "transcript", "help", "exit"}
MESSAGES = [{"id": 5, "role": "user", "content": "What is 2+2?", "versions": [5, 9], "version": 1},
            {"id": 6, "role": "assistant", "content": "4"}]


def turn(turn_id="t1", conversation=CID, **extra):
    return {"type": "turn", "id": turn_id, "conversation": conversation, "model": "fake:small", "incognito": False,
            "local_only": False, **extra}


def done(reply="ok", conversation=CID):
    return {"type": "done", "reply": reply, "conversation": conversation, "tools": [], "message_id": 7, "versions": [7],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "context": {"tokens": 100, "window": 1000, "percent": 10.0},
            "model": "fake:small", "tainted": False}


def conversation(fake, messages=()):
    fake.conversations[CID] = {"id": CID, "title": "", "project": None, "model": "", "incognito": False, "local_only": False,
                               "created_at": "", "updated_at": "", "packs": ["web"], "messages": list(messages)}


@pytest.fixture
def w(tmp_path, fake_clara, client):
    return make_agent_world(tmp_path, fake_clara, client)


# ---------------------------------------------------------------------- understanding a line
@pytest.mark.parametrize(("line", "kind", "text"), [
    ("", "empty", ""), ("   ", "empty", ""),
    ("explain this", "message", "explain this"),
    ("//home/me/notes.txt explain", "message", "/home/me/notes.txt explain"),
    ("!!important", "message", "!important"),
    ("!git status", "bang", "git status"),
    ("/model fake:big", "slash", "/model fake:big"),
    ("/ls src", "shell", "ls src"),
    ("/cd src", "unknown", "cd"),
    ("/nope", "unknown", "nope"),
    ("  /exit  ", "slash", "/exit"),
])
def test_a_typed_line_is_understood(w, line, kind, text):
    routed = route(line, w.slash, w.shell_registry)
    assert (routed.kind, routed.text) == (kind, text)


def test_the_commands_are_those_of_the_plan_and_each_has_a_usage(w):
    names = {spec.name for spec in w.slash.specs()}
    assert names >= PLAN_COMMANDS
    for spec in w.slash.specs():
        assert spec.usage.startswith("/" + spec.name) and spec.summary and not spec.summary.endswith(".")
    assert w.slash.get("quit") is w.slash.get("exit") and w.slash.get("?") is w.slash.get("help")
    assert ("model", "Show or choose the model") in w.slash.pairs()


def test_blocked_shell_commands_say_why_and_unknown_ones_suggest(w):
    assert {"cd", "ai", "exit", "reload", "clear"} == BLOCKED_IN_AGENT
    assert "keeps its folder" in w.unknown("cd")
    assert "Did you mean /model" in w.unknown("mo") and "start it with //" in w.unknown("zzz")


# ---------------------------------------------------------------------- help, exit, plan, todo
async def test_help_lists_keys_and_every_command(w):
    text = await w.command("/help")
    assert "Shift+Enter" in text and "@path" in text and "// and !!" in text
    for spec in w.slash.specs():
        assert spec.usage in text


async def test_exit_plan_and_todo(w):
    await w.command("/exit")
    assert w.screen.exited
    assert "Plan mode is on" in await w.command("/plan") and w.agent.session.plan_mode and w.screen.refreshed == 1
    assert "off" in await w.command("/plan off") and not w.agent.session.plan_mode
    assert "usage: /plan" in await w.command("/plan maybe")
    assert "checklist is empty" in await w.command("/todo")


# ---------------------------------------------------------------------- model, tools, project
async def test_model_lists_and_chooses(w):
    assert await w.command("/model") == "" and w.screen.picks[0][2] == "default"  # cancelled: nothing changes
    rows = dict(w.screen.picks[0][1])
    assert next(iter(rows)) == "default" and "fake:big" in rows and "vision" in rows["fake:big"] and "128k" in rows["fake:big"]
    w.screen.choose = "fake:big"
    assert "Model: fake:big" in await w.command("/model") and w.agent.session.model == "fake:big"
    w.screen.choose = None
    await w.command("/model")
    assert w.screen.picks[2][2] == "fake:big"  # the picker opens on the model in use
    assert "Model: fake:big" in await w.command("/model fake:big") and w.agent.session.model == "fake:big"
    assert "Clara's default" in await w.command("/model default")
    assert "/model: No model matches 'zzz'" in await w.command("/model zzz")


async def test_tools_list_switch_and_reset(w):
    listing = await w.command("/tools")
    assert "read" in listing and "second_brain" in listing and "No vault" in listing
    assert "Switched off: run, git" in await w.command("/tools off run git")
    assert w.agent.session.disabled_tools == {"local_run", "local_git"}
    assert "off  run" in (await w.command("/tools")).replace("   ", "  ")
    assert "Switched off: web" in await w.command("/tools off web") and w.agent.session.packs == []
    assert "Switched on: run" in await w.command("/tools on run")
    assert "defaults" in await w.command("/tools reset") and w.agent.session.disabled_tools == set() and w.agent.session.packs is None
    assert "No local tool 'nope'" in await w.command("/tools off nope")
    assert "usage: /tools" in await w.command("/tools off") and "usage: /tools" in await w.command("/tools flip x")


async def test_project_lists_and_chooses(w):
    assert "Demo" in await w.command("/project") and "2 files" in await w.command("/project")
    assert "Project: Demo (for new conversations)" in await w.command("/project 1")
    assert "*" in await w.command("/project")
    assert "No project" in await w.command("/project none") and w.agent.session.project is None
    assert "No project 'zzz'" in await w.command("/project zzz")


# ---------------------------------------------------------------------- the conversation
async def test_clear_starts_over_and_takes_options(w):
    old = w.agent.session
    text = await w.command("/clear --incognito --local-only")
    assert "incognito, local only" in text and w.agent.session is not old and w.agent.session.incognito
    assert w.screen.cleared == 1 and w.screen.refreshed == 1
    assert "unknown option '--forever'" in await w.command("/clear --forever") and w.screen.cleared == 1


async def test_retry_and_version(w, fake_clara):
    assert "no message to answer again" in await w.command("/retry") and w.screen.turns == []
    conversation(fake_clara, MESSAGES)
    w.agent.session.conversation = CID
    assert "no local tools" in await w.command("/retry")
    assert w.screen.turns == [("", "", True)]
    assert "2 versions" in await w.command("/version") and "version 1 is shown" in await w.command("/version")
    assert "Version 2 of 2" in await w.command("/version 2") and w.screen.answers == ["4"]
    assert "usage: /version" in await w.command("/version x") and "2 versions" in await w.command("/version 3")


async def test_restore_lists_and_brings_back(w, fake_clara):
    assert "No saved session" in await w.command("/restore")
    conversation(fake_clara, MESSAGES)
    fake_clara.queue_turn(turn(), {"type": "usage", "prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 0}, done())
    await w.agent.send("hello")
    w.agent.session.title = "Math"
    w.agent.store.save(w.agent.session)
    listing = await w.command("/restore list")
    assert "Math" in listing and "13 tokens" in listing
    w.agent.new_session()
    assert "Restored: Math" in await w.command("/restore 1")
    assert w.screen.cleared == 1 and [m.content for m in w.screen.replayed[0]] == ["What is 2+2?", "4"]
    assert "Choose 1 to 1" in await w.command("/restore 4") and "usage: /restore" in await w.command("/restore x")


async def test_undo_reports_each_file(w, fake_clara):
    assert "Nothing to undo" in await w.command("/undo")
    w.ui.answers = ["allow_once"]
    calls = {"type": "tool_requests", "turn": "t1",
             "calls": [{"id": "c0", "name": "local_write", "arguments": {"path": "a.txt", "content": "1"}}]}
    fake_clara.queue_turn(turn(), calls, WaitResults(), done())
    await w.agent.send("write")
    text = await w.command("/undo")
    assert "deleted" in text and "a.txt" in text and "cannot be undone" in text and not (w.work / "a.txt").exists()


async def test_compact_context_and_usage(w, fake_clara):
    assert "nothing to compact" in await w.command("/compact")
    assert "Context: Clara reports it after the first answer" in await w.command("/context")
    fake_clara.queue_turn(turn(), {"type": "usage", "prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 0}, done())
    await w.agent.send("hi")
    assert "Compacted: 62% to 12%" in await w.command("/compact keep it")
    context = await w.command("/context")
    assert "10% full" in context and "100 of 1,000 tokens" in context and "16 tools" in context and "AGENT.md" in context
    usage = await w.command("/usage")
    assert "session" in usage and "24h" in usage and "7d" in usage and "all" in usage and "10" in usage
    assert "main: 13 tokens" in await w.command("/usage session")
    assert "session, 24h, 7d or all" in await w.command("/usage year")


async def test_init_sends_the_request_and_needs_a_trusted_folder(w, tmp_path, fake_clara, client):
    await w.command("/init")
    assert w.screen.turns == [(INIT_REQUEST, "/init", False)]
    other = make_agent_world(tmp_path / "second", fake_clara, client, trusted=False)
    assert "not trusted" in await other.command("/init") and other.screen.turns == []


# ---------------------------------------------------------------------- permissions and records
async def test_approvals_list_and_answer(w, fake_clara):
    assert "no request waiting" in await w.command("/approvals")
    fake_clara.add_approval(3, tool="ws_write", summary="Write notes.md", level="write")
    listing = await w.command("/approvals")
    assert "#3" in listing and "ws_write" in listing and "Write notes.md" in listing
    assert "Request #3 (ws_write): approved." in await w.command("/approvals 3 allow")
    assert fake_clara.decisions == {3: "allow_once"}
    assert "usage: /approvals" in await w.command("/approvals 3 maybe") and "usage: /approvals" in await w.command("/approvals x")
    assert "Already answered" in await w.command("/approvals 3 never")


async def test_rules_show_both_sides_test_calls_and_remove(w):
    w.agent.rules.add("allow", __import__("custom_console.core.permissions.rules", fromlist=["RuleKey"]).RuleKey("local_run", "ls"))
    text = await w.command("/rules")
    assert "local_run 'ls'" in text and "Clara's rules" in text and "none" in text
    ask = await w.command("/rules test local_write path=x.txt content=hi")
    assert ask.startswith("ASK") and "can be remembered" in ask and not (w.work / "x.txt").exists()
    assert "step: allowlist" in await w.command("/rules test read path=.")
    assert (await w.command("/rules test local_read path=.")).startswith("ALLOW")
    assert "step: tainted" in await w.command("/rules test local_write path=x.txt content=hi --tainted")
    assert "Clara would: ask - default for write" in await w.command("/rules test ws_write path=x")
    assert "not key=value" in await w.command("/rules test local_read oops") and "usage: /rules" in await w.command("/rules nonsense")
    assert "Removed" in await w.command("/rules remove 1") and "No local rule 1" in await w.command("/rules remove 1")


async def test_rules_add_keeps_a_pattern_for_this_folder_under_the_same_limits_as_a_proposed_one(w):
    assert "usage: /rules add" in await w.command("/rules add")
    added = await w.command("/rules add 'uv run pytest:*'")
    assert "Rule 1: local_run 'uv run pytest:*' in this folder" in added
    assert "runs code" in await w.command("/rules add uv:*") and "name of a tool" in await w.command("/rules add uv run")
    assert "runs code" in await w.command("/rules add sh:*") and "shell syntax" in await w.command("/rules add ls > x")
    listing = await w.command("/rules")
    assert "local_run 'uv run pytest:*' in this folder" in listing and "used 0x" in listing
    assert (await w.command("/rules test local_run command='uv run pytest -q'")).startswith("ALLOW")
    assert "step: stored" in await w.command("/rules test local_run command='uv run pytest -q'")
    assert [r.pattern for r in w.agent.rules.list()] == [("uv", "run", "pytest")]


async def test_rules_add_needs_a_trusted_folder(w):
    w.agent.ctx.trusted = False
    assert "not trusted" in await w.command("/rules add 'uv run pytest:*'")
    assert w.agent.rules.list() == []


async def test_journal_and_transcript_open_a_viewer(w, fake_clara):
    assert "empty so far" in await w.command("/journal")
    conversation(fake_clara, MESSAGES)
    fake_clara.queue_turn(turn(), done())
    await w.agent.send("hi")
    await w.command("/journal")
    title, lines = w.screen.views[0]
    assert title.startswith("Journal - session ") and "turn" in lines[0].plain and "ok" in lines[0].plain
    await w.command("/transcript")
    title, lines = w.screen.views[1]
    assert title.startswith("Transcript") and [line.plain for line in lines][:3] == ["-- You --", "What is 2+2?", ""]
    other = make_agent_world(w.root / "other", w.fake, w.agent.client)
    assert "conversation yet" in await other.command("/transcript")


# ---------------------------------------------------------------------- the shell's commands inside the agent
async def test_a_shell_command_runs_in_the_agents_folder_and_cannot_ask_or_move(w):
    (w.work / "notes.txt").write_text("alpha\nbeta\n")
    (w.work / "sub").mkdir()
    shell = new_shell(w.agent.vfs, w.agent.platform, w.agent.layout, env={"PATH": "/usr/bin:/bin"})
    listed = await run_shell_command(shell, "ls -1", CancelToken())
    assert [line.plain for line in listed.lines] == ["sub/", "notes.txt"] and listed.status == 0  # folders first
    piped = await run_shell_command(shell, "cat notes.txt | grep -c a", CancelToken())
    assert [line.plain for line in piped.lines] == ["2"]
    asked = await run_shell_command(shell, "rm -r sub", CancelToken())
    assert (w.work / "sub").is_dir() and any("asks a question" in e for e in asked.errors)
    await run_shell_command(shell, "cd sub", CancelToken())
    assert shell.vfs.cwd == str(w.work) and w.agent.vfs.cwd == str(w.work)  # only the copy moved


def test_an_incognito_agent_shows_in_its_session_flags(tmp_path, fake_clara, client):
    w = make_agent_world(tmp_path, fake_clara, client, options=AgentOptions(incognito=True, local_only=True))
    assert (w.agent.session.incognito, w.agent.session.local_only) == (True, True)
