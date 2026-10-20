"""`local_git`: `status`, `diff` and `log`, read-only.

It runs only in trusted folders, with the file-monitor hook and external diff and text-conversion programs switched off
(a repository's own config can name programs to run, SEC-10), no pager, no optional lock, and the scrubbed environment.
"""

from __future__ import annotations

import shutil
from typing import Any

from ...platform.process import StartError, run_capture
from ..permissions.engine import READ, CallInfo, PathUse
from .base import BOOL, INT, STR, ToolContext, ToolError, ToolSpec, cut_middle, obj
from .files import _resolve

TIMEOUT = 60.0
SAFE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "diff.external=", "-c", "core.askPass=")


def find_git() -> str | None:
    return shutil.which("git")


def _available(ctx: ToolContext) -> str:
    return "" if ctx.git else "git is not installed."


def _inspect(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    path = args.get("path") or "."
    return CallInfo("local_git", READ, (PathUse("."), PathUse(path)), needs_trusted_folder=True,
                    summary=f"git {args.get('action', 'status')} {path}")


def _run(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.git:
        raise ToolError("git is not installed.")
    action = str(args.get("action", "status"))
    if action not in ("status", "diff", "log"):
        raise ToolError("action must be status, diff or log.")
    where = _resolve(ctx, ".")
    command = [ctx.git, *SAFE_CONFIG, "--no-pager", "--no-optional-locks", "-C", where.real()]
    if action == "status":
        command += ["status", "--short", "--branch", "--untracked-files=normal"]
    elif action == "diff":
        command += ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if args.get("staged"):
            command.append("--cached")
    else:
        count = max(1, min(int(args.get("max_count", 20) or 20), 200))
        command += ["log", "--no-ext-diff", "--no-textconv", "--no-color", f"--max-count={count}", "--date=short",
                    "--format=%h %ad %an  %s"]
    path = args.get("path")
    if path:
        target = _resolve(ctx, path)
        command += ["--", target.real()]
    env = dict(ctx.env)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_CONFIG_NOSYSTEM": "1"})
    for name in ("GIT_EXTERNAL_DIFF", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_ASKPASS", "GIT_SSH_COMMAND", "GIT_SSH"):
        env.pop(name, None)
    try:
        done = run_capture(command, cwd=where.real(), env=env, timeout=TIMEOUT, cancel=ctx.cancel)
    except StartError as error:
        raise ToolError(str(error)) from None
    text = done.output.rstrip("\n")
    if done.timed_out:
        raise ToolError("git took more than a minute and was stopped.")
    if not done.ok:
        raise ToolError(cut_middle(text or f"git exited with {done.returncode}", 2000))
    return cut_middle(text, ctx.result_limit) if text else "(nothing to show)"


GIT_TOOL = ToolSpec(
    "local_git",
    "Read-only git in the working folder: `status`, `diff` (add staged=true for the index) or `log`. Optionally for one path.",
    obj({"action": {**STR, "enum": ["status", "diff", "log"]}, "path": STR, "staged": BOOL,
         "max_count": {**INT, "description": "Commits for log (default 20, at most 200)"}}, ["action"]),
    READ, _inspect, _run, read_only_for_subagents=True, needs_trusted_folder=True, available=_available)
