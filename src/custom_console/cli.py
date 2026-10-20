"""The entry point: options, start-up checks, `--save-token`. Every problem is printed in one list."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .clara import ClaraClient, ClaraError, ConnectionFailed, TokenFileError, read_token
from .clara.types import TOKEN_SHAPE
from .core.log import setup_logging
from .core.runtime import Runtime
from .paths import prepare_folders
from .platform import Platform, current
from .settings import OPTION_NAMES, Settings, SettingsError, load

CHECK_TIMEOUT = 4.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custom_console", description="A terminal shell and a full-screen agent console for Clara.")
    parser.add_argument("--save-token", action="store_true", help="ask for the Clara token (hidden) and save it")
    parser.add_argument("--debug", action="store_true", help="log request and response metadata (never bodies or headers)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--url", metavar="URL", help="Clara's address (CLARA_URL)")
    parser.add_argument("--token-file", metavar="PATH", help="where the token is read (CLARA_TOKEN_FILE)")
    parser.add_argument("--data-dir", metavar="PATH", help="where sessions, undo records and the journal live (CONSOLE_DATA_DIR)")
    parser.add_argument("--tool-timeout", metavar="SECONDS", help="stop a foreground command after this long (CONSOLE_TOOL_TIMEOUT)")
    parser.add_argument("--result-limit", metavar="CHARS", help="characters kept of one tool result (CONSOLE_RESULT_LIMIT)")
    parser.add_argument("--rmapi", metavar="PATH", help="path to rmapi (CONSOLE_RMAPI)")
    parser.add_argument("--log-level", metavar="LEVEL", help="debug, info, warning or error (CONSOLE_LOG_LEVEL)")
    return parser


def options_from(args: argparse.Namespace) -> dict[str, str]:
    given = {}
    for flag, name in OPTION_NAMES.items():
        value = getattr(args, flag.lstrip("-").replace("-", "_"))
        if value is not None:
            given[name] = value
    return given


def token_checker(platform: Platform) -> Callable[[Path], str | None]:
    """The check the settings run on the token file: readable, private, and shaped like a token."""
    def check(path: Path) -> str | None:
        try:
            read_token(path, platform.privacy.problem)
        except TokenFileError as error:
            return str(error.message)
        return None
    return check


def print_problems(problems: Sequence[str], out: Callable[[str], None]) -> None:
    out("The console cannot start:")
    for problem in problems:
        out(f"  - {problem}")


def save_token(settings: Settings, platform: Platform, *, ask: Callable[[str], str] = getpass.getpass,
               out: Callable[[str], None] = print) -> int:
    """Ask for the token with hidden input and write it where only you can read it. The token is never printed."""
    token = ask("Clara token (input hidden): ").strip()
    if not TOKEN_SHAPE.match(token):
        out("That does not look like a Clara token (clara_<id>_<secret>). Nothing was saved.")
        return 1
    path = settings.token_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        platform.privacy.write_private(path, token + "\n")
    except OSError as error:
        out(f"Could not write {path}: {error.strerror or error}")
        return 1
    problem = platform.privacy.problem(path)
    if problem:
        out(f"Saved to {path}, but: {problem}")
        return 1
    out(f"Saved the token to {path} (readable by you only).")
    return 0


async def check_clara(client: ClaraClient) -> tuple[bool, str]:
    """Ask Clara who we are. Returns (usable, one line to show)."""
    try:
        me = await asyncio.wait_for(client.me(), CHECK_TIMEOUT)
    except ClaraError as error:
        if error.code == "unauthorized":
            return False, "Clara refused the token: it is wrong or revoked. Run `custom_console --save-token`."
        if isinstance(error, ConnectionFailed):
            return False, f"Clara is not reachable at {client.url}. The shell works; `ai agent` needs it."
        return False, str(error)
    except TimeoutError:
        return False, f"Clara at {client.url} did not answer. The shell works; `ai agent` needs it."
    return True, f"Clara: connected as token '{me.token}' ({me.client})."


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platform = current()
    options = options_from(args)
    try:
        if args.save_token:
            return save_token(load(options, debug=args.debug), platform)
        settings = load(options, debug=args.debug, token_check=token_checker(platform))
    except SettingsError as error:
        print_problems(error.problems, lambda line: print(line, file=sys.stderr))
        return 2

    notes = [f"Warning: {w}" for w in settings.warnings]
    notes += prepare_folders(settings.data, platform.privacy)
    logger = setup_logging(settings.data.log, settings.log_level, debug=settings.debug)
    token = read_token(settings.token_file, platform.privacy.problem)
    runtime = Runtime(settings, platform, token, logger, notes)

    async def status() -> tuple[bool, str]:
        async with runtime.new_client() as client:
            return await check_clara(client)

    usable, line = asyncio.run(status())
    runtime.notes.append(line)
    logger.info("start: clara %s", "usable" if usable else "not usable")

    from .ui.shell_app import run_shell
    return run_shell(runtime)
