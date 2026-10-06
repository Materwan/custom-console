import pytest

from custom_console.settings import DEFAULT_INSTRUCTIONS, load_settings


def load(env, tmp_path):
    return load_settings(env, root=tmp_path, use_dotenv=False)


def test_defaults_are_relative_to_the_root(tmp_path):
    s = load({}, tmp_path)
    assert s.project_root == tmp_path.resolve()
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.agent_log_path == s.data_dir / "logs" / "agent.jsonl"
    assert s.agent_usage_path == s.agent_dir / "usage.jsonl"
    assert s.agent_sessions_dir == s.agent_dir / "sessions" and s.agent_keep_sessions == 5
    assert s.agent_checkpoints_dir == s.agent_dir / "checkpoints"
    assert s.agent_project_file == "AGENT.md"
    assert s.clara_url == "http://127.0.0.1:8765" and s.clara_token is None
    assert s.clara_user_name is None and s.agent_user_id == "default_user"
    assert s.agent_permission_level == 1
    assert s.rmapi_path is None and not s.rmapi_available
    assert s.moodle_enabled and s.smtp_host is None


def test_empty_values_mean_not_set(tmp_path):
    env = {"DATA_DIR": "", "AGENT_PERMISSION_LEVEL": "  ", "RMAPI_PATH": "", "WSL_DISTRO": ""}
    s = load(env, tmp_path)
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.agent_permission_level == 1
    assert s.rmapi_path is None
    assert s.wsl_distro == "Ubuntu"


def test_overrides(tmp_path):
    env = {
        "DATA_DIR": str(tmp_path / "elsewhere"),
        "AGENT_PERMISSION_LEVEL": "2",
        "OLLAMA_HOST": "http://host:1234/",
        "MOODLE_ENABLED": "false",
        "SMTP_PORT": "2525",
        "WSL_DISTRO": "Debian",
        "AGENT_PROJECT_FILE": "NOTES.md",
        "CLARA_URL": "http://server:9000/",
        "CLARA_TOKEN": "chat-token",
        "CLARA_USER_NAME": "Erwan",
    }
    s = load(env, tmp_path)
    assert s.data_dir == (tmp_path / "elsewhere").resolve()
    assert s.agent_permission_level == 2
    assert s.ollama_host == "http://host:1234"
    assert s.moodle_enabled is False
    assert s.smtp_port == 2525
    assert s.wsl_distro == "Debian"
    assert s.agent_project_file == "NOTES.md"
    assert (s.clara_url, s.clara_token, s.clara_user_name) == (
        "http://server:9000",
        "chat-token",
        "Erwan",
    )


def test_invalid_integer_is_reported_with_the_variable_name(tmp_path):
    with pytest.raises(ValueError, match="AGENT_PERMISSION_LEVEL"):
        load({"AGENT_PERMISSION_LEVEL": "lots"}, tmp_path)


def test_rmapi_available_only_when_the_file_exists(tmp_path):
    exe = tmp_path / "rmapi.exe"
    assert not load({"RMAPI_PATH": str(exe)}, tmp_path).rmapi_available
    exe.write_text("x")
    assert load({"RMAPI_PATH": str(exe)}, tmp_path).rmapi_available


def test_instructions_fall_back_when_the_file_is_missing_or_empty(tmp_path):
    assert load({}, tmp_path).load_instructions() == DEFAULT_INSTRUCTIONS
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agent_instructions.txt").write_text("   ")
    assert load({}, tmp_path).load_instructions() == DEFAULT_INSTRUCTIONS


def test_instructions_are_read_from_the_configured_file(tmp_path):
    custom = tmp_path / "mine.txt"
    custom.write_text("Be brief.\n", encoding="utf-8")
    assert load({"AGENT_INSTRUCTIONS_PATH": str(custom)}, tmp_path).load_instructions() == "Be brief."


def test_dotenv_file_is_loaded_without_overriding_the_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("AGENT_DEFAULT_MODEL=from-dotenv\nWSL_DISTRO=FromDotenv\n")
    monkeypatch.delenv("AGENT_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("WSL_DISTRO", "FromEnv")
    s = load_settings(root=tmp_path)
    assert s.default_model == "from-dotenv"
    assert s.wsl_distro == "FromEnv"
    monkeypatch.delenv("AGENT_DEFAULT_MODEL", raising=False)


def test_a_user_who_signs_in_speaks_as_themselves(tmp_path):
    s = load({"CLARA_USER": " Erwan ", "CLARA_PASSWORD": "pw", "AGENT_USER_ID": "someone-else"}, tmp_path)
    assert (s.clara_user, s.clara_password, s.agent_user_id) == ("erwan", "pw", "erwan")
    s = load({}, tmp_path)
    assert (s.clara_user, s.clara_password, s.agent_user_id) == (None, None, "default_user")
