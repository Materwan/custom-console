"""API keys of the model providers.

A key is looked up in the environment first (``OPENAI_API_KEY``... possibly from
the ``.env`` file), then among the keys saved with :meth:`KeyStore.save`, which
live in the operating system's credential store (the Windows Credential Manager,
through the ``keyring`` package), never in a plain file.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

SERVICE = "custom-console"  # the name the keys are saved under in the credential store

FROM_ENVIRONMENT = "environment"
SAVED = "saved"


class KeyStore:
    """`env` and `backend` (an object with keyring's get/set/delete_password) are for tests."""

    def __init__(self, env: Optional[Mapping[str, str]] = None, backend: Any = None) -> None:
        self._env = env
        self._backend = backend

    def _environment(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _keyring(self) -> Any:
        if self._backend is None:
            try:
                import keyring
            except ImportError as error:
                raise RuntimeError("saving API keys needs the keyring package: pip install keyring") from error
            self._backend = keyring
        return self._backend

    def _saved(self, variable: str) -> Optional[str]:
        try:
            return self._keyring().get_password(SERVICE, variable) or None
        except Exception:  # no credential store, or it failed: as if nothing was saved
            return None

    def get(self, variable: str) -> Optional[str]:
        """The key named `variable` (e.g. ``OPENAI_API_KEY``), or None."""
        value = (self._environment().get(variable) or "").strip()
        return value or self._saved(variable)

    def source(self, variable: str) -> str:
        """Where the key comes from: FROM_ENVIRONMENT, SAVED, or "" when there is none."""
        if (self._environment().get(variable) or "").strip():
            return FROM_ENVIRONMENT
        return SAVED if self._saved(variable) else ""

    def save(self, variable: str, key: str) -> None:
        self._keyring().set_password(SERVICE, variable, key.strip())

    def forget(self, variable: str) -> bool:
        """Delete the saved key. False when there was none."""
        if not self._saved(variable):
            return False
        self._keyring().delete_password(SERVICE, variable)
        return True
