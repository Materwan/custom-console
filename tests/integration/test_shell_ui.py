"""The shell's prompt, driven with simulated keystrokes (no terminal), and the whole program under a pseudo-terminal."""

from __future__ import annotations

import asyncio
import os
import pty
import re
import select
import sys
import time
from pathlib import Path

import pytest
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from custom_console.commands.completion.engine import CompletionEngine
from custom_console.core.runtime import Runtime
from custom_console.settings import load
from custom_console.ui.shell_app import (
    HistorySuggester,
    SavedHistory,
    Screen,
    ToolbarState,
    clean_temp,
    handle_request,
    location_line,
    make_session,
    read_plain_lines,
    run_command,
    run_loop,
)
from tests.support.shelltest import Sh

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals")


@pytest.fixture
def world(tmp_path):
    sh = Sh(tmp_path)
    for name in ("notes.txt", "numbers.txt", "src/main.py"):
        sh.write(name, "x")
    return sh, CompletionEngine(sh.shell)


def prompt(sh, engine, keys: str) -> str:
    """Type `keys` at the prompt. After each Tab the completion is given time to arrive, as it is when a person types."""
    async def go() -> str:
        with create_pipe_input() as pipe:
            session = make_session(sh.shell, engine, input=pipe, output=DummyOutput())
            task = asyncio.ensure_future(session.prompt_async())
            await asyncio.sleep(0.05)
            for part in re.split(r"(\t|\x1b\[[A-D])", keys):
                if part:
                    if part.startswith("\x1b"):
                        await asyncio.sleep(0.3)  # the ghost text of what was typed is worked out in the background
                    pipe.send_text(part)
                    await asyncio.sleep(0.6 if part == "\t" else 0.05)
            return await asyncio.wait_for(task, 10)

    return asyncio.run(go())


def test_tab_completes_a_single_candidate(world):
    sh, engine = world
    assert prompt(sh, engine, "hist\t\r") == "history"
    assert prompt(sh, engine, "cat src/ma\t\r") == "cat src/main.py"
    assert prompt(sh, engine, "cd s\t\r") == "cd src/"


def test_tab_inserts_the_common_part_when_there_are_several(world):
    sh, engine = world
    sh.write("nothing.md", "x")
    assert prompt(sh, engine, "cat nu\t\r") == "cat numbers.txt"
    assert prompt(sh, engine, "cat n\t\r") == "cat n"                    # notes.txt / nothing.md / numbers.txt share only the 'n'
    assert prompt(sh, engine, "cat not\t\r") == "cat not"                # notes.txt / nothing.md share only 'not'


def test_a_name_with_spaces_is_quoted_by_tab(world):
    sh, engine = world
    sh.write("my file.txt", "x")
    assert prompt(sh, engine, "cat my\t\r") == 'cat "my file.txt"'


def test_the_ghost_text_of_the_newest_history_line_is_accepted_with_the_right_arrow(world):
    sh, engine = world
    sh.shell.history.add("ls /C:/Users")
    assert prompt(sh, engine, "ls /C\x1b[C\r") == "ls /C:/Users"          # Right arrow accepts the suggestion
    assert prompt(sh, engine, "ls /C\r") == "ls /C"                       # without it, only what was typed


def test_ghost_text_is_offered_only_at_the_end_of_the_line(world):
    sh, engine = world
    sh.shell.history.add("echo hello")
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    suggester = HistorySuggester(engine)
    assert suggester.get_suggestion(Buffer(), Document("echo", 4)).text == " hello"
    assert suggester.get_suggestion(Buffer(), Document("echo", 2)) is None
    assert suggester.get_suggestion(Buffer(), Document("zzz", 3)) is None


def test_history_is_walked_with_up_and_saved_without_secrets(world):
    sh, engine = world
    sh.shell.history.add("first")
    sh.shell.history.add("second")
    assert prompt(sh, engine, "\x1b[A\r") == "second"
    assert prompt(sh, engine, "\x1b[A\x1b[A\r") == "first"
    history = SavedHistory(sh.shell)
    assert list(history.load_history_strings()) == ["first", "second", "first"]  # newest first; the second prompt added "first" again
    history.store_string("export API_KEY=hunter2hunter2")
    history.store_string("third")
    assert sh.shell.history.entries == ["first", "second", "first", "third"]


def test_escape_closes_the_menu(world):
    sh, engine = world
    session = make_session(sh.shell, engine)
    keys = {tuple(b.keys) for b in session.key_bindings.bindings}  # type: ignore[union-attr]
    assert any("escape" in str(k).lower() or "Escape" in str(k) for k in keys)


def test_the_location_line_shows_the_place_and_its_rights(world):
    sh, _ = world
    parts = list(location_line(sh.shell))
    assert to_plain_text(parts) == f"{sh.work} rwx" and parts[0][0] == "class:location"
    assert [s for s, _t in parts if s.startswith("class:g")] == ["class:granted"] * 3
    os.chmod(sh.work, 0o500)
    try:
        assert to_plain_text(location_line(sh.shell)).endswith("r-x")
    finally:
        os.chmod(sh.work, 0o700)
    sh("cd /")
    assert to_plain_text(location_line(sh.shell)).startswith("/ r")


def test_the_toolbar_state_is_harmless_outside_an_application():
    state = ToolbarState()
    state.set("loading...")
    assert state.text == "loading..."


def test_run_command_turns_ctrl_c_into_a_stopped_command_not_an_exit(world, monkeypatch):
    sh, _ = world
    screen = Screen()
    errors = []
    screen.err = errors.append  # type: ignore[method-assign]

    def interrupted(shell, line):
        raise KeyboardInterrupt

    monkeypatch.setattr("custom_console.ui.shell_app.run_line", interrupted)
    assert run_command(sh.shell, "sleep", screen) == 130 and errors == ["interrupted"] and sh.shell.last_status == 130
    assert sh.shell.vfs.cancel.cancelled  # the token of that command was cancelled, so a tool in a thread stops too


def test_each_command_gets_a_fresh_cancel_token(world):
    sh, _ = world
    screen = Screen()
    before = sh.shell.vfs.cancel
    run_command(sh.shell, "pwd", screen)
    assert sh.shell.vfs.cancel is not before and not sh.shell.vfs.cancel.cancelled


def runtime_for(sh, tmp_path) -> Runtime:
    settings = load({"CONSOLE_DATA_DIR": str(tmp_path / "data")}, env={}, home=sh.home, config_dir=tmp_path / "cfg")
    return Runtime(settings, sh.shell.platform, "clara_a_0123456789", notes=["Warning: plain http", "Clara: connected."])


def test_the_loop_runs_lines_prints_notes_and_ends_on_exit(world, tmp_path, capsys):
    sh, engine = world
    runtime = runtime_for(sh, tmp_path)
    screen = Screen()
    printed = []
    sh.shell.out = lambda line: printed.append(line.plain)
    lines = iter(["pwd", "   ", "ls", "exit", "pwd"])
    lines_prompt = lambda: next(lines)  # noqa: E731
    # drive run_loop through its plain-input path
    import builtins
    original = builtins.input
    builtins.input = lambda prompt="": lines_prompt()
    try:
        status = run_loop(runtime, sh.shell, engine, screen, None)
    finally:
        builtins.input = original
    assert status == 0 and printed[0] == str(sh.work) and "src/" in printed[1]
    assert printed.count(str(sh.work)) == 1, "nothing runs after `exit`"
    shown = capsys.readouterr().out
    assert "Warning: plain http" in shown and "Clara: connected." in shown


def test_requests_clear_reload_and_agent(world, tmp_path, monkeypatch):
    sh, _ = world
    runtime = runtime_for(sh, tmp_path)
    screen = Screen()
    errors = []
    screen.err = errors.append  # type: ignore[method-assign]
    for line, keeps_going in (("clear", True), ("exit", False), ("reload", False)):
        run_command(sh.shell, line, screen)
        assert handle_request(runtime, sh.shell, screen) is keeps_going
    assert sh.desktop.restarted is not None
    opened = []  # the real full-screen agent needs a terminal: tests/integration/test_program_agent.py runs it under one
    monkeypatch.setattr("custom_console.ui.agent_app.run_agent", lambda rt, shell, options: opened.append((rt, shell, options)))
    run_command(sh.shell, "ai agent --incognito --model fake:big", screen)
    assert handle_request(runtime, sh.shell, screen) is True and errors == []
    [(seen_runtime, seen_shell, options)] = opened
    assert seen_runtime is runtime and seen_shell is sh.shell
    assert (options.incognito, options.local_only, options.model) == (True, False, "fake:big")


def test_a_plain_source_is_read_to_its_end():
    items = iter(["a", "b"])

    def source() -> str:
        try:
            return next(items)
        except StopIteration:
            raise EOFError from None

    assert list(read_plain_lines(source)) == ["a", "b"]


def test_old_downloads_are_cleaned_up(tmp_path):
    old, new = tmp_path / "old.pdf", tmp_path / "new.pdf"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    assert clean_temp(tmp_path) == 1 and not old.exists() and new.exists()
    assert clean_temp(tmp_path / "missing") == 0


# ---------------------------------------------------------------------- the whole program, under a pseudo-terminal
def drive(argv: list[str], env: dict[str, str], keys: list[tuple[bytes, float]], cwd: Path, settle: float = 1.5) -> tuple[str, int]:
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(sys.executable, [sys.executable, "-B", "-m", "custom_console", *argv], env)
    out = b""

    def drain(seconds: float) -> None:
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                out += chunk

    drain(settle)
    for data, wait in keys:
        os.write(fd, data)
        drain(wait)
    drain(0.5)
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
        if _ == 0:
            os.kill(pid, 15)
            os.waitpid(pid, 0)
            status = -1
    except ChildProcessError:
        status = 0
    return out.decode("utf-8", "replace"), os.waitstatus_to_exitcode(status) if status != -1 else -1


def test_the_whole_program_under_a_terminal(tmp_path, fake_clara):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    (work / "notes.txt").write_text("alpha\nbeta\n")
    token = home / ".config" / "clara" / "token"
    token.parent.mkdir(parents=True)
    token.write_text(fake_clara.token + "\n")
    token.chmod(0o600)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
           "TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30", "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
           "PYTHONIOENCODING": "utf-8"}
    for name in [n for n in env if n.startswith(("CONSOLE_", "CLARA_"))]:
        del env[name]
    out, code = drive(["--url", fake_clara.url], env,
                      [(b"cat notes.txt | grep -c a\r", 1.0), (b"cat no\t", 1.0), (b"\x15", 0.3),
                       (b"ls -l\r", 1.0), (b"alias ll='ls -1'\r", 0.8), (b"ll\r", 0.8), (b"exit\r", 1.5)], cwd=work)
    assert "Clara: connected as token" in out, out[-800:]
    assert "notes.txt" in out and "2\r\n" in out                          # cat | grep -c a  -> 2 (alpha, beta both have an 'a')
    assert code == 0, f"exit code {code}\n{out[-600:]}"
    assert fake_clara.token not in out
    saved = (home / ".local" / "share" / "custom-console" / "history").read_text().splitlines()
    assert saved == ["cat notes.txt | grep -c a", "ls -l", "alias ll='ls -1'", "ll", "exit"]
