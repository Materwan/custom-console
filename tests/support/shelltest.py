"""A shell over a temp world, driven by typed lines, with the output captured."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from custom_console.commands.base import Line
from custom_console.commands.pipes import run_line
from custom_console.commands.shell import new_shell
from custom_console.core.vfs.ops import Vfs
from custom_console.paths import DataLayout
from custom_console.platform import for_system
from tests.support.guard import active
from tests.support.harness import FakeDesktop, SandboxPaths, sandbox_resolver


class Result:
    def __init__(self, out: list[Line], err: list[str], status: int):
        self.lines = out
        self.out = [line.plain for line in out]
        self.err = err
        self.status = status

    @property
    def text(self) -> str:
        return "\n".join(self.out)


class Sh:
    def __init__(self, tmp_path: Path, remarkable=None, answers: list[bool] | None = None,  # type: ignore[no-untyped-def]
                 platform_home: Path | None = None):
        assert active(), "the safety net (tests/support/guard.py) is not installed: refusing to build a shell"
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.work = self.home / "work"
        self.work.mkdir(parents=True)
        self.desktop = FakeDesktop()
        base = for_system("linux")
        # the platform's idea of home is a temp folder too (a different one on request, to test that both are protected)
        platform = replace(base, desktop=self.desktop, paths=SandboxPaths(base.paths, platform_home or self.home))
        self.layout = DataLayout(tmp_path / "data")
        self.vfs = Vfs(sandbox_resolver(tmp_path, self.home, platform, remarkable=remarkable), cwd=str(self.work))
        self.shell = new_shell(self.vfs, platform, self.layout, remarkable=remarkable, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)})
        self.out: list[Line] = []
        self.err: list[str] = []
        self.questions: list[str] = []
        self.answers = answers if answers is not None else []
        self.shell.out = self.out.append
        self.shell.err = self.err.append
        self.shell.confirm = self._confirm
        self.shell.width = lambda: 60

    def _confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else False

    def inside_sandbox(self, typed: str) -> bool:
        """Does this typed path, as the shell would resolve it, really lie inside the temp folder? Destructive tests ask first."""
        import os
        real = self.vfs.resolve(typed).real()
        root = os.path.realpath(self.root)
        return real == root or real.startswith(root + os.sep)

    def __call__(self, line: str) -> Result:
        self.out.clear()
        self.err.clear()
        status = run_line(self.shell, line)
        return Result(list(self.out), list(self.err), status)

    def write(self, name: str, text: str = "") -> Path:
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path
