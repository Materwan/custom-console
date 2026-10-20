from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from custom_console.core.tools.base import Question, ToolError, validate_args
from custom_console.core.tools.desktop import extension, is_passive
from custom_console.core.tools.pdf import parse_pages
from custom_console.core.tools.registry import ALL_TOOLS, ToolRegistry
from tests.support.harness import make_world

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")
GIT = shutil.which("git")


@pytest.fixture
def w(tmp_path):
    return make_world(tmp_path)


# ---------------------------------------------------------------------- local_run
async def test_run_reports_the_exit_code_and_output(w):
    out = await w.run("local_run", command="echo hello; echo oops >&2; exit 3")
    assert out.splitlines()[0].startswith("exit code 3") and "hello" in out and "oops" in out
    assert (await w.run("local_run", command="true")).endswith("(no output)")
    (w.work / "sub").mkdir()
    assert (w.work / "sub").name in await w.run("local_run", command="pwd", cwd="sub")


async def test_run_gives_children_a_scrubbed_environment(w, monkeypatch):  # SEC-1
    monkeypatch.setenv("MY_API_TOKEN", "t0ps3cret")
    monkeypatch.setenv("CLARA_URL", "http://x")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("HARMLESS", "visible")
    w2 = make_world(w.root / "second")  # a world built after the variables were set
    out = await w2.run("local_run", command="env")
    assert "HARMLESS=visible" in out
    for hidden in ("t0ps3cret", "hunter2", "CLARA_URL", "MY_API_TOKEN", "DB_PASSWORD"):
        assert hidden not in out


async def test_run_stops_at_the_timeout_and_says_how_to_do_better(w):
    begun = time.monotonic()
    out = await w.run("local_run", command="sleep 30", timeout=1)
    assert time.monotonic() - begun < 10 and "Timed out after 1s" in out and "background=true" in out


async def test_run_cannot_outlive_the_round(w):
    w.ctx.round_deadline = time.monotonic() + 7.0
    begun = time.monotonic()
    out = await w.run("local_run", command="sleep 30", timeout=100)
    assert time.monotonic() - begun < 10 and "Timed out after 2s" in out


async def test_run_in_a_bad_place_or_with_an_empty_command(w):
    (w.work / "f").write_text("")
    with pytest.raises(ToolError, match="not a folder"):
        await w.run("local_run", command="ls", cwd="f")
    with pytest.raises(ToolError, match="empty"):
        await w.run("local_run", command="  ")
    with pytest.raises(ToolError, match="not a folder"):
        await w.run("local_run", command="ls", cwd="nope")


async def test_a_cancelled_run_stops_its_process(w):
    threading.Timer(0.5, w.ctx.cancel.cancel).start()
    begun = time.monotonic()
    out = await w.run("local_run", command="sleep 30")
    assert time.monotonic() - begun < 10 and "Stopped" in out


async def test_long_output_keeps_both_ends(w):
    w.ctx.result_limit = 2000
    out = await w.run("local_run", command="seq 1 5000")
    assert "1\n" in out and "5000" in out and "characters left out" in out and len(out) < 2400


async def test_background_jobs(w):
    started = await w.run("local_run", command="echo first; sleep 30", background=True)
    job_id = started.split()[2].rstrip(":")
    for _ in range(100):
        if "first" in await w.run("local_job", id=job_id, action="output"):
            break
        await asyncio.sleep(0.05)
    assert "running" in await w.run("local_job", id=job_id)
    assert job_id in await w.run("local_job")
    assert "Stopped" in await w.run("local_job", id=job_id, action="stop")
    assert "Already ended" in await w.run("local_job", id=job_id, action="stop")
    with pytest.raises(ToolError, match="No job"):
        await w.run("local_job", id="zzz")
    with pytest.raises(ToolError, match="status, output or stop"):
        await w.run("local_job", id=job_id, action="explode")


async def test_too_many_background_jobs(w):
    ids = [(await w.run("local_run", command="sleep 30", background=True)).split()[2].rstrip(":") for _ in range(8)]
    with pytest.raises(ToolError, match="8 background jobs"):
        await w.run("local_run", command="sleep 30", background=True)
    w.ctx.jobs.stop_all()
    assert len(ids) == 8


# ---------------------------------------------------------------------- local_git (SEC-10)
def git(cwd, *args, env=None):
    base = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", **(env or {})}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=base)


@pytest.mark.skipif(GIT is None, reason="git is not installed")
async def test_git_status_diff_log(w):
    git(w.work, "init", "-q", "-b", "main")
    (w.work / "a.txt").write_text("one\n")
    git(w.work, "add", ".")
    git(w.work, "commit", "-qm", "first commit")
    (w.work / "a.txt").write_text("two\n")
    (w.work / "new.txt").write_text("")
    assert "## main" in await w.run("local_git", action="status") and "?? new.txt" in await w.run("local_git", action="status")
    diff = await w.run("local_git", action="diff")
    assert "-one" in diff and "+two" in diff
    assert "first commit" in await w.run("local_git", action="log", max_count=5)
    assert await w.run("local_git", action="diff", staged=True) == "(nothing to show)"
    assert "-one" in await w.run("local_git", action="diff", path="a.txt")
    with pytest.raises(ToolError, match="status, diff or log"):
        await w.run("local_git", action="push")


@pytest.mark.skipif(GIT is None, reason="git is not installed")
async def test_a_hostile_repository_config_cannot_run_a_program(w):
    git(w.work, "init", "-q", "-b", "main")
    (w.work / "a.txt").write_text("one\n")
    git(w.work, "add", ".")
    git(w.work, "commit", "-qm", "c")
    marker = w.root / "PWNED"
    script = w.root / "evil.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    script.chmod(0o755)
    with (w.work / ".git" / "config").open("a") as config:
        config.write(f"[core]\n\tfsmonitor = {script}\n[diff]\n\texternal = {script}\n[diff \"evil\"]\n\ttextconv = {script}\n")
    (w.work / ".gitattributes").write_text("*.txt diff=evil\n")
    (w.work / "a.txt").write_text("two\n")
    # the control: plain git does run the configured programs (so the test proves something)
    subprocess.run(["git", "diff"], cwd=w.work, capture_output=True, env={**os.environ, "GIT_PAGER": "cat"})
    subprocess.run(["git", "status"], cwd=w.work, capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not run the configured programs, so there is nothing to prove")
    marker.unlink()
    for action in ("status", "diff", "log"):
        await w.run("local_git", action=action)
    assert not marker.exists(), "the repository's own config ran a program"


async def test_git_is_unavailable_when_not_installed(w):
    w.ctx.git = None
    assert "git is not installed" in w.registry.get("local_git").available(w.ctx)
    with pytest.raises(ToolError, match="not installed"):
        await w.run("local_git", action="status")


# ---------------------------------------------------------------------- desktop
@pytest.mark.parametrize("name", ["a.pdf", "a.PNG", "x.docx", "movie.mkv", "notes.txt", "data.csv", "book.epub", "a.b.c.pdf"])
def test_passive_types_may_be_opened(name):
    assert is_passive(name)


@pytest.mark.parametrize("name", [
    "a.exe", "a.bat", "a.cmd", "a.com", "a.ps1", "a.vbs", "a.js", "a.jar", "a.py", "a.pyw", "a.msc", "a.chm", "a.docm",
    "a.xlsm", "a.pptm", "a.appx", "a.msix", "a.application", "a.gadget", "a.psm1", "a.wsh", "a.sct", "a.lnk", "a.url",
    "a.reg", "a.hta", "a.cpl", "a.scr", "a.msi", "a.sh", "a.command", "a.app", "a.dmg", "a.iso", "a.zip", "a.svg", "noext",
    "x.cmd.", "x.cmd ", "x.cmd. .", "report.pdf.exe", "README", ".bashrc",
])
def test_everything_else_is_refused_even_with_a_trailing_dot(name):  # SEC-5
    assert not is_passive(name)


def test_extension_ignores_what_windows_ignores():
    assert extension("x.cmd.") == ".cmd" and extension("C:\\a\\b.TXT") == ".txt" and extension("") == ""


async def test_open_path(w):
    (w.work / "a.pdf").write_text("")
    (w.work / "run.sh").write_text("")
    (w.work / "dir").mkdir()
    assert "Opened" in await w.run("local_open_path", path="a.pdf")
    assert "Opened" in await w.run("local_open_path", path="dir")
    assert [p.name for p in w.desktop.opened] == ["a.pdf", "dir"]
    with pytest.raises(ToolError, match="not opened by the agent"):
        await w.run("local_open_path", path="run.sh")
    with pytest.raises(ToolError, match="No such file"):
        await w.run("local_open_path", path="none.pdf")
    w.desktop.fail = "xdg-open is not installed"
    with pytest.raises(ToolError, match="xdg-open"):
        await w.run("local_open_path", path="a.pdf")


async def test_launch_app_and_clipboard(w):
    assert "Started Editor" in await w.run("local_launch_app", name="editor")
    assert w.desktop.started[0].target == "/usr/bin/editor"
    with pytest.raises(ToolError, match="No application called"):
        await w.run("local_launch_app", name="missing")
    assert await w.run("local_clipboard_read") == "clip text"
    assert "now holds 5" in await w.run("local_clipboard_write", text="hello")
    assert w.desktop.clipboard == "hello"
    w.desktop.fail = "No clipboard tool found"
    with pytest.raises(ToolError, match="No clipboard tool"):
        await w.run("local_clipboard_read")


# ---------------------------------------------------------------------- ask, todo, task, pdf
async def test_ask_user(w):
    with pytest.raises(ToolError, match="no one to ask"):
        await w.run("local_ask_user", question="Which?")
    seen = []

    async def ask(question: Question) -> str:
        seen.append(question)
        return "blue"

    w.ctx.ask = ask
    assert await w.run("local_ask_user", question="Colour?", options=["red", "blue"]) == "The person answered: blue"
    assert seen[0].options == ("red", "blue")
    with pytest.raises(ToolError, match="list of strings"):
        await w.run("local_ask_user", question="x", options=[1])


async def test_todo_write(w):
    out = await w.run("local_todo_write", todos=[{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"},
                                                  {"content": "c"}])
    assert "[x] a" in out and "[~] b" in out and "[ ] c" in out
    assert [t.status for t in w.ctx.todos.items] == ["completed", "in_progress", "pending"]
    with pytest.raises(ToolError, match="status must be"):
        await w.run("local_todo_write", todos=[{"content": "a", "status": "done"}])
    with pytest.raises(ToolError, match="non-empty content"):
        await w.run("local_todo_write", todos=[{"content": " "}])
    with pytest.raises(ToolError, match="at most 50"):
        await w.run("local_todo_write", todos=[{"content": "x"}] * 51)


async def test_task_needs_a_sub_agent_and_cannot_nest(w):
    with pytest.raises(ToolError, match="cannot start another"):
        await w.run("local_task", prompt="do it")

    async def sub(prompt: str, cancel) -> str:
        return f"done: {prompt}"

    w.ctx.subagent = sub
    assert await w.run("local_task", prompt="look") == "done: look"


def test_pdf_pages():
    assert parse_pages("1-3,5") == [0, 1, 2, 4]
    assert parse_pages(" 2 , 2-3 ") == [1, 2]
    for bad in ("0", "3-1", "a", "1-"):
        with pytest.raises(ToolError):
            parse_pages(bad)


# ---------------------------------------------------------------------- registry and schemas
def test_sixteen_tools_with_the_local_prefix_and_unique_names():
    registry = ToolRegistry()
    assert len(registry.names) == 16 and len(set(registry.names)) == 16
    assert all(n.startswith("local_") for n in registry.names)
    for spec in ALL_TOOLS:
        schema = spec.schema()["function"]
        assert schema["name"] == spec.name and schema["description"] and schema["parameters"]["type"] == "object"
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])
    with pytest.raises(ValueError, match="starts with local_"):
        ToolRegistry([ToolRegistry().get("local_list").__class__("bad", "d", {}, "read", lambda c, a: None, lambda c, a: "")])


def test_what_is_offered_depends_on_the_mode(w):
    registry = w.registry
    names = lambda **kw: {s.name for s in registry.offered(w.ctx, **kw)}  # noqa: E731
    everything = names()
    assert "local_write" in everything and "local_run" in everything
    assert "local_write" not in names(plan_mode=True) and "local_run" not in names(plan_mode=True)
    assert {"local_read", "local_ask_user", "local_todo_write", "local_task"} <= names(plan_mode=True)
    assert "local_read" not in names(disabled=frozenset({"local_read"}))
    assert names(subagent=True) == {"local_list", "local_read", "local_search", "local_git", "local_pdf_to_markdown"} & everything
    w.ctx.trusted = False
    untrusted = names()
    assert not {"local_write", "local_edit", "local_run", "local_job", "local_git"} & untrusted
    assert {"local_read", "local_list", "local_open_path"} <= untrusted
    reasons = {s.spec.name: s.reason for s in registry.status(w.ctx, disabled=frozenset({"local_read"}))}
    assert reasons["local_read"] == "switched off with /tools" and "not trusted" in reasons["local_write"]


def test_argument_validation():
    schema = ToolRegistry().get("local_read").parameters
    assert validate_args(schema, {"path": "a"}) == []
    assert validate_args(schema, {}) == ["path is required"]
    assert validate_args(schema, {"path": 1}) == ["path must be string"]
    assert validate_args(schema, {"path": "a", "limit": True}) == ["limit must be integer"]
    assert validate_args(schema, {"path": "a", "bogus": 1}) == ["bogus is not a parameter of this tool"]
    assert validate_args(schema, "text") == ["the arguments must be an object"]
    assert validate_args(schema, {"path": "a", "limit": None}) == []
