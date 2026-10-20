"""Running a parsed pipeline.

Built-in commands read lines from their input and write lines to their output, so `cat notes.txt | grep todo | wc -l` works,
and `<`, `>`, `>>` read or write a path on any backend that allows it. An external program runs as it does in any shell,
attached to the terminal; a pipe or redirection that includes one is refused with a message saying so.

`>` does not replace a file that already exists unless this session created it or read all of it; `>|` does (the same
overwrite guard as the agent's write tool). `>>` appends.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Iterator

from ..core.permissions.redact import is_secret_name, redact
from ..core.vfs.backend import VfsError
from ..core.vfs.text import decode_text, split_lines
from ..platform import Cancelled
from ..platform.process import StartError, run_attached
from . import globbing
from .base import CommandError, CommandSpec, Context, Line, ParseError, Shell, UsageError
from .parser import Pipeline, Word, parse


def describe(error: VfsError) -> str:
    return f"{error.path}: {error.message}" if error.path else error.message


def format_error(error: Exception) -> str:
    return describe(error) if isinstance(error, VfsError) else str(error)


def run_line(shell: Shell, line: str) -> int:
    """Parse and run one typed line. Returns the exit status (2 for a line that cannot be understood)."""
    try:
        pipeline = parse(line, windows=shell.platform.is_windows, home=str(shell.vfs.resolver.home_path), aliases=shell.aliases)
    except ParseError as error:
        shell.err(f"console: {error}")
        shell.last_status = 2
        return 2
    if pipeline is None:
        return shell.last_status
    status = run_pipeline(shell, pipeline)
    shell.last_status = status
    return status


def run_pipeline(shell: Shell, pipeline: Pipeline) -> int:
    specs = [shell.registry.get(stage[0].text) for stage in pipeline.stages]
    external = [stage[0].text for stage, spec in zip(pipeline.stages, specs, strict=True) if spec is None]
    if external:
        if len(pipeline.stages) > 1 or pipeline.redirected:
            shell.err(f"console: pipes and redirection work between built-in commands only; {external[0]} is an external program")
            return 2
        return run_external(shell, pipeline.stages[0])

    stdin: Iterator[Line] | None = None
    if pipeline.stdin is not None:
        try:
            stdin = iter(read_lines(shell, pipeline.stdin.text))
        except (VfsError, CommandError) as error:
            shell.err(f"console: {format_error(error)}")
            return 1
    contexts: list[Context] = []
    output: Iterator[Line] = iter(())
    for index, (words, spec) in enumerate(zip(pipeline.stages, specs, strict=True)):
        assert spec is not None
        last = index == len(pipeline.stages) - 1
        ctx = Context(shell, spec.name, stdin, piped_out=not last or pipeline.stdout is not None)
        contexts.append(ctx)
        output = invoke(ctx, spec, words)
        stdin = output
    if pipeline.stdout is not None:
        redirect_output(contexts[-1], pipeline, output)
    else:
        for line in output:
            shell.out(line)
    return max(c.status for c in contexts[-1:])


def expand_words(shell: Shell, spec: CommandSpec, words: list[Word]) -> list[str]:
    out: list[str] = []
    positional = 0
    after_dashes = False
    ignore_case = shell.platform.paths.case_insensitive
    for word in words:
        if word.text == "--" and not word.quoted:
            after_dashes = True
            out.append(word.text)
            continue
        if not after_dashes and not word.quoted and word.text.startswith("-") and word.text != "-":
            out.append(word.text)  # a flag
            continue
        if spec.globs and word.glob and not word.quoted and positional >= spec.glob_skip:
            out.extend(globbing.expand(shell.vfs, word.text, windows=shell.platform.is_windows, ignore_case=ignore_case))
        else:
            out.append(word.text)
        positional += 1
    return out


def invoke(ctx: Context, spec: CommandSpec, words: list[Word]) -> Iterator[Line]:
    """Parse the arguments of a built-in and start it; the lines come out as they are produced."""
    try:
        argv = expand_words(ctx.shell, spec, words[1:])
        parser = spec.parser()
        if any(a in ("-h", "--help") for a in argv):
            return iter([Line.of(text) for text in parser.format_help().rstrip().split("\n")])
        namespace = parser.parse_args(argv)
        result = spec.run(ctx, namespace)
    except UsageError as error:
        ctx.fail(str(error), 2)
        return iter(())
    except (CommandError, VfsError) as error:
        ctx.fail(format_error(error))
        return iter(())
    return guarded(ctx, result)


def guarded(ctx: Context, result: Iterable[Line] | None) -> Iterator[Line]:
    try:
        yield from result or ()
    except UsageError as error:
        ctx.fail(str(error), 2)
    except (CommandError, VfsError) as error:
        ctx.fail(format_error(error))
    except Cancelled:
        ctx.fail("interrupted", 130)


# ---------------------------------------------------------------------- files
def read_lines(shell: Shell, typed: str) -> list[Line]:
    target = shell.vfs.resolve(typed)
    file = shell.vfs.read_text(target)
    return [Line.of(text) for text in split_lines(file.text)]


def redirect_output(ctx: Context, pipeline: Pipeline, lines: Iterator[Line]) -> None:
    shell = ctx.shell
    redirect = pipeline.stdout
    assert redirect is not None
    text = "".join(line.plain + "\n" for line in lines)
    try:
        target = shell.vfs.resolve(redirect.target.text)
        info = shell.vfs.stat(target)
        if info is not None and info.is_dir:
            ctx.fail(f"{target.virtual} is a folder")
            return
        if redirect.kind == ">>" and info is not None:
            data = shell.vfs.read_bytes(target) + text.encode("utf-8")
        else:
            if redirect.kind == ">" and info is not None and shell.guard.refusal(target.real(), info, target.virtual):
                ctx.fail(f"{target.virtual} exists: use '>|' to replace it or '>>' to add to it")
                return
            data = text.encode("utf-8")
        shell.vfs.write_bytes(target, data)
        made = shell.vfs.stat(target)
        if made is not None and target.on_disk:
            if info is None:
                shell.guard.note_created(target.real())
            shell.guard.note_written(target.real(), made, len(split_lines(decode_text(data))))
    except (VfsError, CommandError) as error:
        ctx.fail(format_error(error))


# ---------------------------------------------------------------------- external programs
def run_external(shell: Shell, words: list[Word]) -> int:
    name = words[0].text
    expanded: list[str] = [name]
    ignore_case = shell.platform.paths.case_insensitive
    for word in words[1:]:
        if word.glob and not word.quoted:
            expanded.extend(globbing.expand(shell.vfs, word.text, windows=shell.platform.is_windows, ignore_case=ignore_case))
        else:
            expanded.append(word.text)
    folder = shell.vfs.here()
    cwd = folder.real() if folder.on_disk and os.path.isdir(folder.real()) else None
    program = _find_program(shell, name)
    if program is None:
        shell.err(f"{name}: command not found")
        return 127
    expanded[0] = program
    env = {k: v for k, v in (shell.env or os.environ).items() if not k.upper().startswith("CLARA_")}
    try:
        return run_attached(expanded, cwd=cwd, env=env)
    except StartError as error:
        shell.err(str(error))
        return 126


def _find_program(shell: Shell, name: str) -> str | None:
    if "/" in name or "\\" in name or name.startswith("."):
        try:
            candidate = shell.vfs.resolve(name).real()
        except VfsError:
            return None
        return candidate if os.path.isfile(candidate) and (shell.platform.is_windows or os.access(candidate, os.X_OK)) else None
    return shutil.which(name, path=(shell.env or os.environ).get("PATH"))


def environment_lines(env: dict[str, str]) -> list[str]:
    """`NAME=value` for each variable, sorted, with the value of anything that looks like a secret hidden."""
    return [f"{name}={'********' if is_secret_name(name) else redact(env[name])}" for name in sorted(env, key=str.lower)]
