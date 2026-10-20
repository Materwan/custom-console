from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from custom_console import paths
from custom_console.platform import for_system
from custom_console.settings import SettingsError, is_loopback, load, parse_env_file

HOME = Path("/home/u")


def test_locations_per_platform():
    linux = paths.locations("linux", {}, HOME)
    assert linux.config == HOME / ".config" / "custom-console"
    assert linux.data == HOME / ".local" / "share" / "custom-console"
    assert linux.token_file == HOME / ".config" / "clara" / "token"
    xdg = paths.locations("linux", {"XDG_CONFIG_HOME": "/x/c", "XDG_DATA_HOME": "/x/d"}, HOME)
    assert xdg.config == Path("/x/c/custom-console") and xdg.token_file == Path("/x/c/clara/token")
    win = paths.locations("windows", {"APPDATA": "C:/R", "LOCALAPPDATA": "C:/L"}, HOME)
    assert win.token_file == Path("C:/R/clara/token") and win.data == Path("C:/L/custom-console/data")
    mac = paths.locations("macos", {}, HOME)
    assert mac.config == HOME / "Library" / "Application Support" / "custom-console"


def load_for(tmp_path, env=None, options=None, dotenv=None, **kwargs):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    if dotenv is not None:
        (config / ".env").write_text(dotenv)
    return load(options, env=env or {}, system="linux", home=tmp_path / "home", config_dir=config, **kwargs)


def test_defaults(tmp_path):
    s = load_for(tmp_path)
    assert s.clara_url == "http://127.0.0.1:8765"
    assert (s.tool_timeout, s.result_limit, s.log_level, s.rmapi) == (120.0, 30_000, "info", None)
    assert s.token_file == tmp_path / "home" / ".config" / "clara" / "token"
    assert s.warnings == ()


def test_the_four_sources_in_order(tmp_path):
    dotenv = "CONSOLE_TOOL_TIMEOUT=10\nCONSOLE_RESULT_LIMIT=5000\nCONSOLE_LOG_LEVEL=debug\n"
    s = load_for(tmp_path, dotenv=dotenv)
    assert (s.tool_timeout, s.result_limit, s.log_level) == (10.0, 5000, "debug")
    s = load_for(tmp_path, dotenv=dotenv, env={"CONSOLE_TOOL_TIMEOUT": "20", "CONSOLE_LOG_LEVEL": "warning"})
    assert (s.tool_timeout, s.result_limit, s.log_level) == (20.0, 5000, "warning")
    s = load_for(tmp_path, dotenv=dotenv, env={"CONSOLE_TOOL_TIMEOUT": "20"}, options={"CONSOLE_TOOL_TIMEOUT": "30"})
    assert s.tool_timeout == 30.0


def test_env_file_never_reaches_the_environment(tmp_path):
    before = dict(os.environ)
    load_for(tmp_path, dotenv="CONSOLE_LOG_LEVEL=debug\n")
    assert dict(os.environ) == before


def test_every_problem_is_reported_at_once(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_for(tmp_path, dotenv="CONSOLE_TOOL_TIMEOUT=soon\nCONSOLE_RESULT_LIMT=5\nTYPO\nCLARA_URL=ftp://x\n",
                 env={"CONSOLE_DATA_DIRR": "/x", "CONSOLE_LOG_LEVEL": "loud"})
    text = "\n".join(caught.value.problems)
    assert len(caught.value.problems) == 6, text
    assert "CONSOLE_TOOL_TIMEOUT" in text and "soon" in text
    assert "Did you mean CONSOLE_RESULT_LIMIT?" in text
    assert "line 3: expected NAME=value" in text
    assert "CLARA_URL" in text and "CONSOLE_LOG_LEVEL" in text
    assert "Did you mean CONSOLE_DATA_DIR?" in text


@pytest.mark.parametrize("line", ["CLARA_TOKEN=clara_a_b", "SMTP_PASSWORD=x", "MY_SECRET=1", "OPENAI_API_KEY=k", "SOME_KEY=1"])
def test_a_secret_in_dotenv_stops_the_start_and_points_to_the_token_file(tmp_path, line):
    with pytest.raises(SettingsError) as caught:
        load_for(tmp_path, dotenv=line + "\n")
    assert "looks like a secret" in caught.value.problems[0] and "--save-token" in caught.value.problems[0]


def test_a_token_value_under_an_innocent_name_is_refused_too(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_for(tmp_path, dotenv="CLARA_URL=clara_abc123_0123456789abcdef\n")
    assert any("looks like a Clara token" in p for p in caught.value.problems)


@pytest.mark.parametrize(("name", "value"), [
    ("CONSOLE_TOOL_TIMEOUT", "0"), ("CONSOLE_TOOL_TIMEOUT", "601"), ("CONSOLE_RESULT_LIMIT", "10"),
    ("CONSOLE_RESULT_LIMIT", "1.5"), ("CONSOLE_LOG_LEVEL", "noisy"), ("CLARA_URL", "127.0.0.1:8765"),
    ("CLARA_URL", "http://user:pw@host"),
])
def test_out_of_range_and_wrong_types_are_refused(tmp_path, name, value):
    with pytest.raises(SettingsError):
        load_for(tmp_path, env={name: value})


def test_empty_values_mean_not_set(tmp_path):
    s = load_for(tmp_path, dotenv="CONSOLE_RMAPI=\nCLARA_URL=\n")
    assert s.rmapi is None and s.clara_url == "http://127.0.0.1:8765"


def test_the_url_loses_its_trailing_slash_and_plain_http_to_another_host_warns(tmp_path):
    assert load_for(tmp_path, env={"CLARA_URL": "https://clara.example/"}).clara_url == "https://clara.example"
    for url in ("http://192.168.1.20:8765", "http://10.0.0.5", "http://clara.lan"):
        assert "plain http://" in load_for(tmp_path, env={"CLARA_URL": url}).warnings[0], url
    for url in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765", "https://clara.example"):
        assert load_for(tmp_path, env={"CLARA_URL": url}).warnings == (), url


def test_loopback():
    assert is_loopback("127.0.0.5") and is_loopback("localhost") and is_loopback("[::1]")
    assert not is_loopback("192.168.0.1") and not is_loopback("example.com")


def test_an_unreadable_token_file_is_one_of_the_problems(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_for(tmp_path, env={"CONSOLE_LOG_LEVEL": "loud"}, token_check=lambda p: f"{p} can be read by others")
    assert len(caught.value.problems) == 2
    assert load_for(tmp_path, token_check=lambda p: None)


def test_env_file_parser():
    pairs, bad = parse_env_file(
        "\ufeff# comment\n\nexport A=1\nB = two words # note\nC='single # kept'\nD=\"dq \\\"x\\\" \\\\ \\n\" # tail\n"
        "E=\nF='open\nG=\"a\" junk\n")
    assert {k: v.value for k, v in pairs.items()} == {"A": "1", "B": "two words", "C": "single # kept",
                                                       "D": 'dq "x" \\ \n', "E": ""}
    assert len(bad) == 2 and "never closed" in bad[0] and "unexpected text" in bad[1]


def test_prepare_folders_tightens_on_posix(tmp_path):
    layout = paths.DataLayout(tmp_path / "data")
    privacy = for_system("linux").privacy
    layout.root.mkdir(mode=0o755)
    os.chmod(layout.root, 0o755)
    notes = paths.prepare_folders(layout, privacy)
    assert any("Tightened" in n and str(layout.root) in n for n in notes)
    assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o700 for f in layout.folders())
    assert paths.prepare_folders(layout, privacy) == []
