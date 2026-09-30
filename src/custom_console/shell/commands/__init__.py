"""Shell commands. Each module exposes a ``register(registry)`` function."""

from . import ai, files, system
from .base import (
    APP,
    MODEL,
    PATH,
    Command,
    CommandError,
    CommandRegistry,
    HelpRequested,
    ShellContext,
    ShellParser,
)


def build_registry() -> CommandRegistry:
    registry = CommandRegistry()
    for module in (files, system, ai):
        module.register(registry)
    return registry


__all__ = [
    "APP",
    "MODEL",
    "PATH",
    "Command",
    "CommandError",
    "CommandRegistry",
    "HelpRequested",
    "ShellContext",
    "ShellParser",
    "build_registry",
]
