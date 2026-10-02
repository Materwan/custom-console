from __future__ import annotations

import pytest

from custom_console.agent.cache import JsonCache
from custom_console.agent.checkpoints import Checkpoints
from custom_console.agent.permissions import PermissionGate
from custom_console.agent.tools import ToolContext
from custom_console.agent.zone import FreeZone
from custom_console.fs import FileManager
from custom_console.settings import load_settings


class GateLog:
    """Records the permission requests and answers them as configured."""

    def __init__(self, answer: bool = True):
        self.answer = answer
        self.asked = []
        self.recorded = []

    def ask(self, info: str) -> bool:
        self.asked.append(info)
        return self.answer

    def record(self, info: str, status: str) -> None:
        self.recorded.append((info, status))


@pytest.fixture
def make_ctx(tmp_path):
    """Factory: `make_ctx(auto_level=2, answer=True, zone=False, **env)` -> (ToolContext, GateLog).

    With `zone=True` the folder the agent starts in (`tmp_path/files`) is the free zone.
    """

    def factory(auto_level: int = 2, answer: bool = True, zone: bool = False, **env):
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        settings = load_settings(env, root=root, use_dotenv=False)
        log = GateLog(answer)
        start = tmp_path / "files"
        start.mkdir(exist_ok=True)
        ctx = ToolContext(
            settings=settings,
            files=FileManager(start_dir=str(start)),
            gate=PermissionGate(auto_level, log.ask, log.record),
            cache=JsonCache(settings.agent_cache_path),
            zone=FreeZone(start) if zone else FreeZone(),
            checkpoints=Checkpoints(tmp_path / "checkpoints"),
        )
        return ctx, log

    return factory


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()[0]


@pytest.fixture
def files_dir(tmp_path):
    (tmp_path / "files").mkdir(exist_ok=True)
    return tmp_path / "files"
