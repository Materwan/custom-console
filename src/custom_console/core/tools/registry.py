"""The 16 local tools: their schemas, permission classes and handlers, in one place.

`ToolRegistry.offered()` is what is sent to Clara: a tool that is switched off, unavailable, not allowed in this mode or in
an untrusted folder is simply not sent. Sixteen schemas are far below the 64 Clara accepts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..permissions.engine import READ, SAFE
from .ask import ASK_TOOLS
from .base import ToolContext, ToolSpec
from .desktop import DESKTOP_TOOLS
from .files import FILE_TOOLS
from .git import GIT_TOOL
from .pdf import PDF_TOOL
from .run import JOB_TOOL, RUN_TOOL

ALL_TOOLS: tuple[ToolSpec, ...] = (*FILE_TOOLS, RUN_TOOL, JOB_TOOL, GIT_TOOL, PDF_TOOL, *DESKTOP_TOOLS, *ASK_TOOLS)
PREFIX = "local_"


@dataclass(frozen=True, slots=True)
class ToolStatus:
    spec: ToolSpec
    offered: bool
    reason: str  # why not, when it is not


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ALL_TOOLS):
        self.specs = tuple(specs)
        self._by_name = {s.name: s for s in self.specs}
        for name in self._by_name:
            if not name.startswith(PREFIX):
                raise ValueError(f"{name}: a local tool's name starts with {PREFIX}")
        if len(self._by_name) != len(self.specs):
            raise ValueError("two tools share a name")

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def status(self, ctx: ToolContext, *, disabled: frozenset[str] = frozenset(), plan_mode: bool = False,
               subagent: bool = False) -> list[ToolStatus]:
        out: list[ToolStatus] = []
        for spec in self.specs:
            reason = ""
            if spec.name in disabled:
                reason = "switched off with /tools"
            elif subagent and not spec.read_only_for_subagents:
                reason = "a sub-agent only reads"
            elif plan_mode and spec.kind not in (READ, SAFE):
                reason = "plan mode: read tools only"
            elif spec.needs_trusted_folder and not ctx.trusted:
                reason = "this folder is not trusted"
            elif spec.available is not None and (why := spec.available(ctx)):
                reason = why
            out.append(ToolStatus(spec, not reason, reason))
        return out

    def offered(self, ctx: ToolContext, **kwargs: object) -> list[ToolSpec]:
        return [s.spec for s in self.status(ctx, **kwargs) if s.offered]  # type: ignore[arg-type]

    def schemas(self, ctx: ToolContext, **kwargs: object) -> list[dict[str, object]]:
        return [spec.schema() for spec in self.offered(ctx, **kwargs)]
