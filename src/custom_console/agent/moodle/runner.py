"""Runs Moodle/Playwright calls on one dedicated background thread.

Playwright's sync API manipulates the asyncio event loop policy of the thread
it runs on (Windows needs a ProactorEventLoop to spawn the browser). On the
thread that hosts the terminal UI this would clash with prompt_toolkit's own
event loop. Every Moodle call therefore runs on a single worker thread: all
calls are serialized, and the (non thread-safe) Playwright objects are only
ever touched from that one thread.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from .client import MoodleClient

T = TypeVar("T")

MISSING_DEPENDENCIES = (
    "Moodle tools need the optional dependencies: "
    "pip install 'custom-console[moodle]' && playwright install chromium"
)


class MoodleRunner:
    def __init__(
        self,
        base_url: str,
        state_path,
        confirm_login: Callable[[str], bool],
    ):
        self.base_url = base_url
        self.state_path = state_path
        self._confirm_login = confirm_login
        self._executor: Optional[ThreadPoolExecutor] = None
        self._client = None  # MoodleClient, created on the worker thread
        self._lock = threading.Lock()

    # -- worker thread ------------------------------------------------------ #

    def _get_client(self):
        """Start (once) and return the client. Runs on the worker thread."""
        if self._client is None:
            try:
                from .client import MoodleClient
            except ImportError as error:
                raise RuntimeError(MISSING_DEPENDENCIES) from error

            client = MoodleClient(base_url=self.base_url, state_path=self.state_path)
            client.start(
                on_login_required=lambda: self._confirm_login(
                    "A browser window was opened on Moodle. Log in there, then accept "
                    "to continue (refuse to cancel)."
                )
            )
            self._client = client
        return self._client

    def _close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- public ------------------------------------------------------------- #

    def run(self, fn: Callable[[MoodleClient], T]) -> T:
        """Run `fn(client)` on the Moodle thread and return its result.

        Exceptions raised by `fn` are re-raised on the calling thread.
        """
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moodle")
            executor = self._executor
        return executor.submit(lambda: fn(self._get_client())).result()

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is None:
            return
        try:
            executor.submit(self._close_client).result(timeout=30)
        except Exception:
            pass
        executor.shutdown(wait=True)
