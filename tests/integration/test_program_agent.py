"""The whole program under a terminal: the shell, `ai agent` (full screen, in the same process), and back."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.fake_clara.server import Hang
from tests.support.ptyterm import Pty

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals")
CID = "c0123456789abcdef"


def program_env(home: Path, token: str) -> dict[str, str]:
    (home / ".config" / "clara").mkdir(parents=True)
    file = home / ".config" / "clara" / "token"
    file.write_text(token + "\n")
    file.chmod(0o600)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
           "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "36", "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
           "PYTHONIOENCODING": "utf-8"}
    for name in [n for n in env if n.startswith(("CONSOLE_", "CLARA_"))]:
        del env[name]
    return env


def turn(extra=()):
    return [{"type": "turn", "id": "t1", "conversation": CID, "model": "fake:small", "incognito": False, "local_only": False},
            *extra,
            {"type": "done", "reply": "Hi there", "conversation": CID, "tools": [], "message_id": 7, "versions": [7],
             "usage": {"prompt_tokens": 4, "completion_tokens": 2}, "context": {"tokens": 6, "window": 1000, "percent": 0.6},
             "model": "fake:small", "tainted": False}]


def test_the_shell_opens_the_agent_and_comes_back(tmp_path, fake_clara):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    fake_clara.queue_turn(*turn([{"type": "token", "text": "Hi there"}]))
    term = Pty(["-m", "custom_console", "--url", fake_clara.url], program_env(home, fake_clara.token), work)
    try:
        term.expect("Clara: connected as token")
        term.send("ai agent\r")
        term.expect("Trust ", timeout=20)          # a new folder asks once
        term.send("o")
        term.expect("Folder trusted", timeout=10)
        term.send("hello there\r")
        deadline = 20
        while not fake_clara.requests_to("/v1/chat/stream") and deadline:
            term.read(0.5)
            deadline -= 1
        assert fake_clara.last("/v1/chat/stream")["body"]["message"] == "hello there"
        term.read(2.0)
        mark = term.mark()
        term.send("/exit\r")
        term.expect("Left the agent console", timeout=15, after=mark)
        mark = term.mark()
        term.send("pwd\r")
        term.expect(str(work), timeout=10, after=mark)     # the shell prompt is back and works
        term.send("exit\r")
        assert term.wait_exit() == 0
    finally:
        term.kill()
    assert (home / ".local" / "share" / "custom-console" / "agent_history").read_text().splitlines() == ["hello there", "/exit"]
    assert fake_clara.token not in term.text
    trusted = home / ".local" / "share" / "custom-console" / "trust.json"
    assert trusted.exists() and str(work) not in trusted.read_text()      # a hash, not the path


def test_escape_stops_an_answer_under_a_real_terminal(tmp_path, fake_clara):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    fake_clara.queue_turn({"type": "turn", "id": "t1", "conversation": CID, "model": "fake:small", "incognito": False,
                           "local_only": False}, {"type": "token", "text": "partial"}, Hang())
    term = Pty(["-m", "custom_console", "--url", fake_clara.url], program_env(home, fake_clara.token), work)
    try:
        term.expect("Clara: connected as token")
        term.send("ai agent\r")
        term.expect("Trust ", timeout=20)
        term.send("o")
        term.expect("Folder trusted", timeout=10)
        term.send("long one\r")
        term.expect("working", timeout=15)
        term.send("\x1b")
        term.expect("Stopped.", timeout=10)
        assert fake_clara.disconnects == 1
        term.send("/exit\r")
        term.expect("Left the agent console", timeout=15)
        term.send("exit\r")
        assert term.wait_exit() == 0
    finally:
        term.kill()
