"""The shell's built-in commands, one module per group."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...core.vfs.guard import OverwriteGuard
from ...core.vfs.ops import Vfs
from ...paths import DataLayout
from ...platform import Platform
from ..aliases import Aliases
from ..base import Registry, Shell
from ..history import History
from . import agent, change, desktopcmds, navigate, read, remarkable, system
from .apps import AppCache


def build_registry() -> Registry:
    registry = Registry()
    for module in (navigate, read, change, system, desktopcmds, remarkable, agent):
        for spec in module.COMMANDS:
            registry.add(spec)
    return registry


def new_shell(vfs: Vfs, platform: Platform, layout: DataLayout, *, remarkable: Any = None,
              env: Mapping[str, str] | None = None) -> Shell:
    """A `Shell` with the built-in commands, the saved aliases, history and apps, and a guard of its own."""
    return Shell(vfs=vfs, platform=platform, layout=layout, guard=OverwriteGuard(platform.paths.case_insensitive),
                 aliases=Aliases(layout.aliases), history=History(layout.history), apps=AppCache(layout.apps),
                 registry=build_registry(), env=dict(env or {}), remarkable=remarkable)
