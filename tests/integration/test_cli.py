from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from custom_console import cli
from custom_console.clara import ClaraClient
from custom_console.platform import current
from custom_console.settings import SettingsError, load
from tests.fake_clara.server import FakeClara

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("CONSOLE_", "CLARA_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home" / ".local" / "share"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


@posix_only
def test_save_token_writes_a_private_file_and_never_prints_the_token(env, fake_clara, capsys):
    out = []
    code = cli.save_token(load({}, token_check=None), current(), ask=lambda prompt: fake_clara.token + "\n", out=out.append)
    path = env / "home" / ".config" / "clara" / "token"
    assert code == 0 and path.read_text() == fake_clara.token + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fake_clara.token not in " ".join(out) + capsys.readouterr().out


def test_save_token_refuses_what_is_not_a_token(env):
    out = []
    assert cli.save_token(load({}), current(), ask=lambda _: "hunter2", out=out.append) == 1
    assert "does not look like" in out[0] and not (env / "home" / ".config" / "clara" / "token").exists()


@posix_only
def test_start_reports_every_problem_in_one_list(env, capsys):
    token = env / "home" / ".config" / "clara" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("clara_a_0123456789\n")
    token.chmod(0o644)
    code = cli.main(["--tool-timeout", "soon", "--log-level", "loud"])
    err = capsys.readouterr().err
    assert code == 2 and err.startswith("The console cannot start:")
    assert err.count("\n  - ") == 3 and "chmod 600" in err


def test_a_missing_token_file_points_to_save_token(env, capsys):
    assert cli.main([]) == 2
    assert "--save-token" in capsys.readouterr().err


@posix_only
async def test_check_clara_gives_one_line_per_situation(fake_clara: FakeClara):
    async with ClaraClient(fake_clara.url, fake_clara.token) as good:
        assert await cli.check_clara(good) == (True, "Clara: connected as token 'console' (console).")
    async with ClaraClient(fake_clara.url, "clara_x_wrong") as bad:
        usable, line = await cli.check_clara(bad)
    assert not usable and "--save-token" in line and "\n" not in line
    async with ClaraClient("http://127.0.0.1:1", "clara_x_y") as nowhere:
        usable, line = await cli.check_clara(nowhere)
    assert not usable and "not reachable" in line


@posix_only
def test_a_good_start_reaches_the_shell(env, fake_clara, capsys, monkeypatch):
    token = env / "home" / ".config" / "clara" / "token"
    cli.save_token(load({}), current(), ask=lambda _: fake_clara.token, out=lambda _: None)
    seen = {}

    def fake_shell(runtime):
        seen["runtime"] = runtime
        return 0

    monkeypatch.setattr("custom_console.ui.shell_app.run_shell", fake_shell)
    code = cli.main(["--url", fake_clara.url])
    runtime = seen["runtime"]
    assert code == 0 and runtime.token == fake_clara.token and token.exists()
    assert any("connected as token" in n for n in runtime.notes)
    assert fake_clara.token not in repr(runtime)
    assert fake_clara.token not in capsys.readouterr().out
    assert (env / "home" / ".local" / "share" / "custom-console" / "sessions").is_dir()


def test_options_become_setting_names():
    args = cli.build_parser().parse_args(["--url", "http://x", "--result-limit", "5000", "--debug"])
    assert cli.options_from(args) == {"CLARA_URL": "http://x", "CONSOLE_RESULT_LIMIT": "5000"}
    with pytest.raises(SettingsError):
        load({"CONSOLE_RESULT_LIMIT": "x"})
