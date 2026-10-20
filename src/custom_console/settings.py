"""Settings: four sources in a fixed order, validated all at once.

    1. command-line options   2. environment variables   3. `.env` in the config folder   4. built-in defaults

The `.env` file is parsed, never loaded into `os.environ` (so no child process inherits it), and it holds no secrets:
a key that looks like one stops the start with a pointer to the token file. If anything is wrong, every problem is
reported in one list and nothing starts half-configured.
"""

from __future__ import annotations

import difflib
import ipaddress
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .paths import DataLayout, Locations, locations, system_name

DEFAULT_URL = "http://127.0.0.1:8765"
LOG_LEVELS = ("debug", "info", "warning", "error")
SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|BEARER|API_?KEY|PRIVATE_?KEY|_KEY$)", re.I)
TOKEN_VALUE = re.compile(r"clara_[A-Za-z0-9]+_[A-Za-z0-9_\-]{8,}")
TOKEN_HINT = "The Clara token lives in its own file: run `custom_console --save-token`."


class SettingsError(Exception):
    """One or more problems with the settings, all of them."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Settings:
    clara_url: str
    token_file: Path
    data_dir: Path
    config_dir: Path
    tool_timeout: float = 120.0
    result_limit: int = 30_000
    rmapi: str | None = None
    log_level: str = "info"
    debug: bool = False
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def data(self) -> DataLayout:
        return DataLayout(self.data_dir)


@dataclass(frozen=True, slots=True)
class Raw:
    value: str
    source: str  # "the command line", "the environment", ".env line 3"


def _number(low: float, high: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"must be a number from {low:g} to {high:g}, not {text!r}") from None
        if not low <= value <= high:
            raise ValueError(f"must be from {low:g} to {high:g}, not {text}")
        return value
    return parse


def _integer(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"must be a whole number from {low:,} to {high:,}, not {text!r}") from None
        if not low <= value <= high:
            raise ValueError(f"must be from {low:,} to {high:,}, not {text}")
        return value
    return parse


def _level(text: str) -> str:
    if text.lower() not in LOG_LEVELS:
        raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}, not {text!r}")
    return text.lower()


def _path(text: str) -> Path:
    return Path(text).expanduser().absolute()


def _url(text: str) -> str:
    parts = urlsplit(text.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"must be an address such as {DEFAULT_URL}, not {text!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("must be a plain address: no user, query or fragment")
    return text.strip().rstrip("/")


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# name -> (parser, what it is)
FIELDS: dict[str, tuple[Callable[[str], object], str]] = {
    "CLARA_URL": (_url, "Clara's address"),
    "CLARA_TOKEN_FILE": (_path, "where the token is read"),
    "CONSOLE_DATA_DIR": (_path, "where sessions, undo records, the journal and rules live"),
    "CONSOLE_TOOL_TIMEOUT": (_number(1, 600), "seconds before a foreground command is stopped"),
    "CONSOLE_RESULT_LIMIT": (_integer(1_000, 1_000_000), "characters kept of one tool result"),
    "CONSOLE_RMAPI": (_path, "path to rmapi"),
    "CONSOLE_LOG_LEVEL": (_level, "log detail"),
}

# command-line option -> setting name (`--save-token` and `--debug` are handled by the entry point)
OPTION_NAMES = {
    "--url": "CLARA_URL", "--token-file": "CLARA_TOKEN_FILE", "--data-dir": "CONSOLE_DATA_DIR",
    "--tool-timeout": "CONSOLE_TOOL_TIMEOUT", "--result-limit": "CONSOLE_RESULT_LIMIT", "--rmapi": "CONSOLE_RMAPI",
    "--log-level": "CONSOLE_LOG_LEVEL",
}


def parse_env_file(text: str) -> tuple[dict[str, Raw], list[str]]:
    """`NAME=value` lines (comments, `export`, quotes, an inline ` # comment`). Returns the pairs and the bad lines."""
    pairs: dict[str, Raw] = {}
    problems: list[str] = []
    for number, line in enumerate(text.lstrip("﻿").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export ").lstrip()
        name, equals, rest = stripped.partition("=")
        name = name.strip()
        if not equals or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            problems.append(f".env line {number}: expected NAME=value.")
            continue
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            quote = rest[0]
            end = rest.find(quote, 1)
            while quote == '"' and end > 0 and rest[end - 1] == "\\":
                end = rest.find(quote, end + 1)
            if end < 0:
                problems.append(f".env line {number}: the quote opened for {name} is never closed.")
                continue
            value = rest[1:end]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
            tail = rest[end + 1:].strip()
            if tail and not tail.startswith("#"):
                problems.append(f".env line {number}: unexpected text after the closing quote of {name}.")
                continue
        else:
            value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
        pairs[name] = Raw(value, f".env line {number}")
    return pairs, problems


def _suggest(name: str) -> str:
    close = difflib.get_close_matches(name.upper(), FIELDS, n=1, cutoff=0.6)
    return f" Did you mean {close[0]}?" if close else ""


def _warnings(clara_url: str) -> tuple[str, ...]:
    parts = urlsplit(clara_url)
    if parts.scheme == "http" and parts.hostname and not is_loopback(parts.hostname):
        return (f"CLARA_URL is plain http:// to {parts.hostname}, which is not this computer: your token and messages "
                "cross the network unencrypted. Use https://.",)
    return ()


def load(options: Mapping[str, str] | None = None, *, env: Mapping[str, str] | None = None, system: str | None = None,
         home: Path | None = None, config_dir: Path | None = None, debug: bool = False,
         token_check: Callable[[Path], str | None] | None = None) -> Settings:
    """The settings, or a SettingsError listing every problem.

    `options` maps setting names to values given on the command line. `token_check`, when given, is asked about the
    token file once its path is known and returns a problem or None.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    where: Locations = locations(system or system_name(), env, home)
    folder = config_dir if config_dir is not None else where.config
    problems: list[str] = []
    raw: dict[str, Raw] = {}

    env_file = folder / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as error:
        text = ""
        problems.append(f"Cannot read {env_file}: {getattr(error, 'strerror', None) or error}")
    pairs, bad = parse_env_file(text)
    problems += [f"{env_file}: {line}" if i == 0 else line for i, line in enumerate(bad)]
    for name, item in pairs.items():
        if name in FIELDS:
            raw[name] = item
        elif SECRET_KEY.search(name):
            problems.append(f"{env_file} ({item.source}): {name} looks like a secret, and secrets do not go in .env. "
                            f"{TOKEN_HINT}")
        else:
            problems.append(f"{env_file} ({item.source}): unknown setting {name}.{_suggest(name)}")
        if TOKEN_VALUE.search(item.value):
            problems.append(f"{env_file} ({item.source}): the value of {name} looks like a Clara token. {TOKEN_HINT}")

    for name, value in env.items():
        if name in FIELDS:
            raw[name] = Raw(value, "the environment")
        elif name.startswith("CONSOLE_"):
            problems.append(f"The environment sets {name}, which is not a setting.{_suggest(name)}")
    for name, value in (options or {}).items():
        if name not in FIELDS:
            problems.append(f"Unknown setting {name}.{_suggest(name)}")
        else:
            raw[name] = Raw(value, "the command line")

    values: dict[str, object] = {}
    for name, (parse, _doc) in FIELDS.items():
        chosen = raw.get(name)
        if chosen is None or chosen.value == "":
            continue  # an empty value means "not set"
        try:
            values[name] = parse(chosen.value)
        except ValueError as error:
            problems.append(f"{name} ({chosen.source}): {error}.")

    token_file = values.get("CLARA_TOKEN_FILE", where.token_file)
    assert isinstance(token_file, Path)
    if token_check is not None and not any(p.startswith("CLARA_TOKEN_FILE") for p in problems):
        found = token_check(token_file)
        if found:
            problems.append(found)
    if problems:
        raise SettingsError(problems)

    url = str(values.get("CLARA_URL", DEFAULT_URL))
    data_dir = values.get("CONSOLE_DATA_DIR", where.data)
    rmapi = values.get("CONSOLE_RMAPI")
    timeout = values.get("CONSOLE_TOOL_TIMEOUT", 120.0)
    limit = values.get("CONSOLE_RESULT_LIMIT", 30_000)
    assert isinstance(data_dir, Path) and isinstance(timeout, float) and isinstance(limit, int)
    return Settings(
        clara_url=url, token_file=token_file, data_dir=data_dir, config_dir=folder, tool_timeout=timeout,
        result_limit=limit, rmapi=str(rmapi) if isinstance(rmapi, Path) else None,
        log_level=str(values.get("CONSOLE_LOG_LEVEL", "info")), debug=debug, warnings=_warnings(url))
