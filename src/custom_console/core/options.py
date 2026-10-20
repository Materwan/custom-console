"""What `ai agent` was asked to start with."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentOptions:
    incognito: bool = False
    local_only: bool = False
    model: str | None = None
