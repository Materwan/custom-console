"""The agent console's `/commands`."""

from __future__ import annotations

from . import info, models, session
from .base import SlashRegistry


def build_slash_registry() -> SlashRegistry:
    registry = SlashRegistry()
    for module in (session, models, info):
        for spec in module.COMMANDS:
            registry.add(spec)
    registry.add(info.make_help(registry))
    return registry
