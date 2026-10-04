"""`git_*` tools: what a repository looks like (status, changes, history), without changing it.

They only read, so they are free inside the free zone and auto-accepted at level 1 elsewhere,
unlike `run_command`, which always asks. Arguments that start with "-" are refused: they would be
read by git as options (``--output=...`` writes a file).
"""

import os
import shutil
import subprocess
from typing import Callable, List, Optional

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded, zone_level
from .shell import shorten_output

GIT_TIMEOUT = 30
MAX_LOG = 100


def run_git(arguments: List[str], cwd: str) -> str:
    """git's output (UTF-8 paths, no pager); RuntimeError with what git said when it fails."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not installed (or not in PATH).")
    done = subprocess.run(
        [git, "-C", cwd, "-c", "core.quotepath=false", "--no-pager", *arguments],
        capture_output=True,
        timeout=GIT_TIMEOUT,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", errors="replace").strip() or f"git exited with {done.returncode}"
        raise RuntimeError(message)
    return done.stdout.decode("utf-8", errors="replace").rstrip("\n")


def _no_option(name: str, value: Optional[str]) -> None:
    if value is not None and str(value).strip().startswith("-"):
        raise ValueError(f"{name} must not start with '-'.")


def git_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    if shutil.which("git") is None:
        return []
    files = ctx.files

    def folder(path: str) -> str:
        local = files.local_path(path, "git")
        return local if os.path.isdir(local) else os.path.dirname(local)

    def clip(text: str) -> str:
        return shorten_output(text, ctx.max_chars())

    @guarded(ctx, zone_level(ctx, PermissionLevel.READ, "path"))
    def git_status(path: str = ".") -> ToolResult:
        """The branch, how far it is from its upstream, and the changed, staged and new files
        of the git repository containing `path`.

        Args:
            path: a folder (or file) inside the repository (default: the working directory).
        """
        output = run_git(["status", "--short", "--branch"], folder(path))
        return ToolResult.ok(output or "Nothing to report.")

    @guarded(ctx, zone_level(ctx, PermissionLevel.READ, "path"))
    def git_diff(
        path: str = ".",
        staged: bool = False,
        revision: Optional[str] = None,
        file: Optional[str] = None,
        context: int = 3,
    ) -> ToolResult:
        """The changes of the repository as a unified diff: not yet staged (default), staged,
        or since a revision.

        Args:
            path: a folder inside the repository (default: the working directory).
            staged: the changes staged for the next commit.
            revision: compare with this commit or branch instead (e.g. "HEAD~1", "main").
            file: only this file or folder (relative to `path`).
            context: lines of context around each change.
        """
        _no_option("revision", revision)
        _no_option("file", file)
        arguments = ["diff", f"-U{max(0, min(int(context), 20))}"]
        if staged:
            arguments.append("--staged")
        if revision:
            arguments.append(revision.strip())
        arguments.append("--")
        if file:
            arguments.append(file)
        cwd = folder(path)
        diff = run_git(arguments, cwd)
        if not diff:
            return ToolResult.ok("No change.")
        stat = run_git([*arguments[:1], "--stat", *arguments[2:]], cwd)
        return ToolResult.ok(clip(f"{stat}\n\n{diff}"), summary=stat.splitlines()[-1].strip() if stat else None)

    @guarded(ctx, zone_level(ctx, PermissionLevel.READ, "path"))
    def git_log(path: str = ".", max_count: int = 10, file: Optional[str] = None, revision: Optional[str] = None) -> ToolResult:
        """The latest commits: short hash, date, author and subject, newest first.

        Args:
            path: a folder inside the repository (default: the working directory).
            max_count: how many commits (at most 100).
            file: only the commits that changed this file or folder.
            revision: start from this commit or branch (default: the current one).
        """
        _no_option("revision", revision)
        _no_option("file", file)
        arguments = ["log", f"-n{max(1, min(int(max_count), MAX_LOG))}", "--date=short", "--format=%h %ad %an: %s"]
        if revision:
            arguments.append(revision.strip())
        arguments.append("--")
        if file:
            arguments.append(file)
        return ToolResult.ok(run_git(arguments, folder(path)) or "No commit yet.")

    return [git_status, git_diff, git_log]
