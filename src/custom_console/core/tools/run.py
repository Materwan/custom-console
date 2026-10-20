"""`local_run` and `local_job`: commands, in the foreground with a timeout or in the background.

A foreground command is stopped at its timeout; longer work uses `background: true` and returns a job id that
`local_job` follows. That keeps every round well inside Clara's 900-second wait. Commands run with a scrubbed environment.
"""

from __future__ import annotations

from typing import Any

from ...platform.process import BackgroundJob, StartError, run_capture, start_background
from ..permissions.engine import RUN, SAFE, CallInfo
from .base import BOOL, INT, STR, ToolContext, ToolError, ToolSpec, cut_middle, obj

MAX_TIMEOUT = 600.0
MAX_RUNNING_JOBS = 8
KEEP_BYTES = 2_000_000


class JobTable:
    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}

    def start(self, command: str, cwd: str, env: dict[str, str]) -> BackgroundJob:
        if sum(1 for j in self._jobs.values() if j.running) >= MAX_RUNNING_JOBS:
            raise ToolError(f"{MAX_RUNNING_JOBS} background jobs are already running: stop one with local_job first.")
        try:
            job = start_background(command, cwd=cwd, env=env, keep=KEEP_BYTES)
        except StartError as error:
            raise ToolError(str(error)) from None
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(job_id)

    def all(self) -> list[BackgroundJob]:
        return list(self._jobs.values())

    @property
    def running(self) -> int:
        return sum(1 for j in self._jobs.values() if j.running)

    def stop_all(self) -> None:
        for job in self._jobs.values():
            if job.running:
                job.stop()


def working_directory(ctx: ToolContext, typed: str) -> str:
    where = ctx.vfs.resolve(typed) if typed else ctx.folder
    if not where.on_disk:
        raise ToolError(f"{where.virtual}: commands run on a disk folder, not on the reMarkable or the virtual root.")
    info = ctx.vfs.stat(where)
    if info is None or not info.is_dir:
        raise ToolError(f"{where.virtual}: not a folder.")
    real = where.real()
    if real.startswith("\\\\"):
        raise ToolError("A command cannot run in a WSL folder from Windows: use a drive folder, or run it inside WSL.")
    return real


def _inspect_run(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    command = str(args.get("command", ""))
    where = f" in {args['cwd']}" if args.get("cwd") else ""
    background = " (in the background)" if args.get("background") else ""
    return CallInfo("local_run", RUN, command=command, cwd=str(args.get("cwd") or ""), needs_trusted_folder=True,
                    summary=f"Run: {command}{where}{background}")


def _preview_run(ctx: ToolContext, args: dict[str, Any]) -> str:
    where = ctx.vfs.resolve(args["cwd"]).virtual if args.get("cwd") else ctx.folder.virtual
    return f"$ {args.get('command', '')}\nin {where}\nWhat a command changes cannot be undone with /undo."


def _run_run(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = str(args["command"]).strip()
    if not command:
        raise ToolError("The command is empty.")
    try:
        cwd = working_directory(ctx, str(args.get("cwd") or ""))
    except Exception as error:
        if isinstance(error, ToolError):
            raise
        raise ToolError(str(error)) from None
    env = dict(ctx.env)
    if args.get("background"):
        job = ctx.jobs.start(command, cwd, env)
        return f"Started job {job.id}: {command}\nFollow it with local_job (id {job.id}); stop it with action=stop."
    timeout = float(args.get("timeout") or ctx.tool_timeout)
    timeout = max(1.0, min(timeout, MAX_TIMEOUT))
    left = ctx.seconds_left()
    if left is not None:  # the round must be answered inside Clara's wait
        timeout = max(1.0, min(timeout, left - 5.0))
    try:
        done = run_capture(command, shell=True, cwd=cwd, env=env, timeout=timeout, cancel=ctx.cancel, keep=KEEP_BYTES)
    except StartError as error:
        raise ToolError(str(error)) from None
    body = done.output.rstrip("\n")
    if done.cancelled:
        head = f"Stopped: the person cancelled it after {done.seconds:.1f}s."
    elif done.timed_out:
        head = (f"Timed out after {timeout:.0f}s and was stopped. Run long commands with background=true and follow "
                "them with local_job.")
    else:
        head = f"exit code {done.returncode} ({done.seconds:.1f}s)"
    return f"{head}\n{cut_middle(body, ctx.result_limit)}" if body else f"{head}\n(no output)"


RUN_TOOL = ToolSpec(
    "local_run",
    "Run a shell command on this computer (cmd on Windows, sh elsewhere) in the working folder or `cwd`, with a timeout. "
    "Use background=true for anything slow (builds, servers, long tests) and follow it with local_job. Prefer the dedicated "
    "tools (local_read, local_edit, local_search) over cat, sed or grep.",
    obj({"command": STR, "cwd": {**STR, "description": "Folder to run in (default: the working folder)"},
         "timeout": {**INT, "description": f"Seconds before it is stopped (default 120, at most {int(MAX_TIMEOUT)})"},
         "background": {**BOOL, "description": "Start it and return a job id at once"}}, ["command"]),
    RUN, _inspect_run, _run_run, preview=_preview_run, needs_trusted_folder=True)


def _inspect_job(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_job", SAFE, summary=f"Job {args.get('id', 'list')} {args.get('action', 'status')}")


def _describe(job: BackgroundJob) -> str:
    if job.running:
        state = f"running {job.seconds:.0f}s"
    elif job.stopped:
        state = f"stopped after {job.seconds:.0f}s"
    else:
        state = f"finished with exit code {job.returncode} after {job.seconds:.0f}s"
    return f"{job.id}  {state}  {job.command}"


def _run_job(ctx: ToolContext, args: dict[str, Any]) -> str:
    job_id = str(args.get("id") or "")
    action = str(args.get("action") or "status")
    if not job_id:
        jobs = ctx.jobs.all()
        return "\n".join(_describe(j) for j in jobs) if jobs else "No background jobs."
    job = ctx.jobs.get(job_id)
    if job is None:
        raise ToolError(f"No job {job_id}. Without an id, local_job lists the jobs.")
    if action == "stop":
        if job.running:
            job.stop()
            return f"Stopped. {_describe(job)}"
        return f"Already ended. {_describe(job)}"
    if action == "output":
        text = job.output().rstrip("\n")
        return f"{_describe(job)}\n{cut_middle(text, ctx.result_limit)}" if text else f"{_describe(job)}\n(no output yet)"
    if action != "status":
        raise ToolError("action must be status, output or stop.")
    return _describe(job)


JOB_TOOL = ToolSpec(
    "local_job",
    "Follow a background command started with local_run: action `status` (default), `output` (what it printed so far) or "
    "`stop`. Without an id it lists the jobs.",
    obj({"id": STR, "action": {**STR, "enum": ["status", "output", "stop"]}}),
    SAFE, _inspect_job, _run_job, needs_trusted_folder=True)
