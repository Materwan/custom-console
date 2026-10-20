from __future__ import annotations

import faulthandler
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from custom_console.clara import ClaraClient
from tests.fake_clara.server import FakeClara
from tests.support import guard

guard.install()  # first of all: from here on no test can remove or rename outside the allowed folders


@pytest.fixture
def fake_clara() -> Iterator[FakeClara]:
    fake = FakeClara()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
async def client(fake_clara: FakeClara) -> AsyncIterator[ClaraClient]:
    async with ClaraClient(fake_clara.url, fake_clara.token, read_timeout=2.0) as c:
        yield c


# ---------------------------------------------------------------------- the real home is never a test's business
REAL_HOME = Path(guard.primary_home())


def _listing() -> list[str]:
    try:
        return sorted(os.listdir(REAL_HOME))
    except OSError:
        return []


@pytest.fixture(autouse=True)
def nobody_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME, USERPROFILE and Path.home() are a temp folder for every test, so no code under test can reach the real one."""
    fake = tmp_path_factory.mktemp("home-of-nobody")
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture(scope="session", autouse=True)
def allow_pytest_temp(tmp_path_factory: pytest.TempPathFactory) -> None:
    guard.allow(tmp_path_factory.getbasetemp())


@pytest.fixture(scope="session", autouse=True)
def real_home_tripwire() -> object:
    """Fails the whole run, loudly, if the real home's top level changed while the tests ran."""
    before = _listing()
    yield
    after = _listing()
    if before != after:
        pytest.fail(f"THE REAL HOME CHANGED DURING THE TESTS. Gone: {sorted(set(before) - set(after))}; new: "
                    f"{sorted(set(after) - set(before))}", pytrace=False)


# ---------------------------------------------------------------------- a test that waits for a terminal, or forever
TEST_TIME_LIMIT = 240  # seconds; the slowest test (the performance budget) takes about twenty


@pytest.fixture(autouse=True)
def no_real_terminal_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never starts a full-screen app or a blocking prompt: it would wait for a terminal that is not there. Screens are
    driven with Textual's `run_test()`, the prompt with a pipe input, the whole program with `tests/support/ptyterm.py`."""
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a test started a real terminal app: use run_test(), a pipe input, or the Pty helper")

    monkeypatch.setattr("textual.app.App.run", refuse)
    monkeypatch.setattr("prompt_toolkit.PromptSession.prompt", refuse)


@pytest.fixture(autouse=True)
def hang_watchdog() -> Iterator[None]:
    """If one test takes longer than the limit, print where every thread is and stop the run, instead of waiting for ever."""
    faulthandler.dump_traceback_later(TEST_TIME_LIMIT, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()
