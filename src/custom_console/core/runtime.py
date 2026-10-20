"""What a running console holds: its settings, the platform, the token (read once) and the way to reach Clara."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..clara import ClaraClient
from ..platform import Platform
from ..settings import Settings


@dataclass
class Runtime:
    settings: Settings
    platform: Platform
    token: str = field(repr=False, default="")  # never shown, never put in the environment
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("custom_console"))
    notes: list[str] = field(default_factory=list)  # lines to show before the first prompt

    def new_client(self, label: str = "console") -> ClaraClient:
        trace = self.logger.debug if self.settings.debug else None
        return ClaraClient(self.settings.clara_url, self.token, client_label=label, trace=trace)
