"""Application settings.

Everything is resolved by :func:`load_settings` (no module-level side effects):
environment variables (optionally loaded from a ``.env`` file) override the
defaults, and empty values are treated as "not set".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

# <root>/src/custom_console/settings.py -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_INSTRUCTIONS = (
    "You are a personal assistant. Use the available tools to carry out the "
    "requested tasks and answer concisely."
)


def _get(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    return _get(env, name) or default


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on", "y", "oui")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _get(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    value = _get(env, name)
    return Path(value).expanduser().resolve() if value else default.resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path

    # reMarkable / virtual file system
    rmapi_path: Optional[Path]
    remarkable_sync_dir: Path
    wsl_distro: str

    # Ollama / agent
    ollama_host: str
    default_model: str
    agent_instructions_path: Path
    agent_permission_level: int
    agent_user_id: str
    agent_session_id: str
    agent_long_term_memory: bool  # an extra model call per turn extracts memories
    agent_max_output_tokens: int  # cap of one answer
    agent_history_tool_calls: int  # old tool calls (and results) kept in the context

    # Moodle
    moodle_enabled: bool
    moodle_base_url: str
    moodle_state_path: Path

    # Email (tool registered only when smtp_host is set)
    smtp_host: Optional[str]
    smtp_port: int
    smtp_user: Optional[str]
    smtp_password: Optional[str]
    smtp_from: Optional[str]

    # -- derived locations -------------------------------------------------- #

    @property
    def agent_dir(self) -> Path:
        return self.data_dir / "agent"

    @property
    def agent_db_path(self) -> Path:
        return self.agent_dir / "memory.db"

    @property
    def agent_cache_path(self) -> Path:
        return self.agent_dir / "cache.json"

    @property
    def agent_history_path(self) -> Path:
        return self.agent_dir / "history.txt"

    @property
    def agent_log_path(self) -> Path:
        return self.data_dir / "logs" / "agent.jsonl"

    @property
    def workspace_roots(self) -> dict[str, Path]:
        """Sandboxed folders the agent may freely read/write."""
        return {
            "result": self.agent_dir / "result",
            "tmp": self.agent_dir / "tmp",
        }

    @property
    def saved_apps_path(self) -> Path:
        return self.data_dir / "saved_apps.json"

    # -- helpers ------------------------------------------------------------ #

    @property
    def rmapi_available(self) -> bool:
        return self.rmapi_path is not None and self.rmapi_path.is_file()

    def load_instructions(self) -> str:
        try:
            text = self.agent_instructions_path.read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_INSTRUCTIONS
        return text or DEFAULT_INSTRUCTIONS


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    *,
    root: Optional[Path] = None,
    use_dotenv: bool = True,
) -> Settings:
    """Build a :class:`Settings` from ``env`` (default: ``os.environ``).

    When ``env`` is omitted, a ``.env`` file found in ``root`` is loaded first
    (without overriding variables that are already set).
    """
    root = (root or Path(os.environ.get("CUSTOM_CONSOLE_HOME") or PROJECT_ROOT)).resolve()

    if env is None:
        if use_dotenv:
            from dotenv import load_dotenv

            load_dotenv(root / ".env")
        env = os.environ

    data_dir = _path(env, "DATA_DIR", root / "data")
    rmapi = _get(env, "RMAPI_PATH")

    return Settings(
        project_root=root,
        data_dir=data_dir,
        rmapi_path=Path(rmapi).expanduser() if rmapi else None,
        remarkable_sync_dir=_path(
            env, "REMARKABLE_SYNC_PATH", data_dir / "reMarkable_sync"
        ),
        wsl_distro=_text(env, "WSL_DISTRO", "Ubuntu"),
        ollama_host=_text(env, "OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        default_model=_text(env, "AGENT_DEFAULT_MODEL", "gemma4"),
        agent_instructions_path=_path(
            env, "AGENT_INSTRUCTIONS_PATH", root / "config" / "agent_instructions.txt"
        ),
        agent_permission_level=_int(env, "AGENT_PERMISSION_LEVEL", 1),
        agent_user_id=_text(env, "AGENT_USER_ID", "default_user"),
        agent_session_id=_text(env, "AGENT_SESSION_ID", "console_session"),
        agent_long_term_memory=_flag(env, "AGENT_LONG_TERM_MEMORY", False),
        agent_max_output_tokens=_int(env, "AGENT_MAX_OUTPUT_TOKENS", 4096),
        agent_history_tool_calls=_int(env, "AGENT_HISTORY_TOOL_CALLS", 3),
        moodle_enabled=_flag(env, "MOODLE_ENABLED", True),
        moodle_base_url=_text(env, "MOODLE_BASE_URL", "https://moodle.epita.fr").rstrip(
            "/"
        ),
        moodle_state_path=_path(
            env, "MOODLE_STATE_PATH", root / "config" / "cookies" / "moodle_state.json"
        ),
        smtp_host=_get(env, "SMTP_HOST"),
        smtp_port=_int(env, "SMTP_PORT", 587),
        smtp_user=_get(env, "SMTP_USER"),
        smtp_password=_get(env, "SMTP_PASSWORD"),
        smtp_from=_get(env, "SMTP_FROM"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded on first use."""
    return load_settings()
