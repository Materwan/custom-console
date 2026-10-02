"""Shell commands. Each module exposes a ``register(registry)`` function."""

from . import ai, files, system
from .base import (
    APP,
    MODEL,
    PATH,
    PROVIDER,
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


def build_file_registry() -> CommandRegistry:
    """Only the file commands (cd, ls, cat...): what the agent offers as /commands."""
    registry = CommandRegistry()
    files.register(registry)
    return registry


__all__ = [
    "APP",
    "MODEL",
    "PATH",
    "PROVIDER",
    "Command",
    "CommandError",
    "CommandRegistry",
    "HelpRequested",
    "ShellContext",
    "ShellParser",
    "build_file_registry",
    "build_registry",
]
