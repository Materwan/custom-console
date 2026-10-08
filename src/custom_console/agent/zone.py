"""The free zone: the folder (and subfolders) where file tools need no permission.

Everywhere else the tools keep their normal permission level. The zone is fixed
when the agent starts; it does not follow the agent's ``cd``. Paths are compared
after resolving symbolic links, so a link inside the zone that points outside
does not widen it.

Some paths inside the zone are never free to *change*: what would run code or
steer the agent later (git hooks, editor tasks, virtual environments, `.env`
files, the project instructions file). Reading them stays free.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

# Folders, anywhere below the zone, whose content is protected
PROTECTED_FOLDERS = frozenset({".git", ".hg", ".svn", ".vscode", ".idea", ".venv", "venv", ".github"})


# Folders and files that hold what opens other things: keys, tokens, passwords, cookies, browser profiles. What
# the agent reads goes to the model of the server (and its provider), so reading these outside the free zone (the
# folder the person started the agent in, which they trust) is asked at every permission level but 2.
SENSITIVE_FOLDERS = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store", ".terraform.d", ".docker", "gcloud",
    "credentials", "cookies", "profiles", "user data", "brave-browser", "keychains", "protect",
})
SENSITIVE_NAMES = re.compile(
    r"^(\.env(\..*)?|\.netrc|_netrc|\.git-credentials|\.npmrc|\.pypirc|\.pgpass|\.my\.cnf|id_(rsa|dsa|ecdsa|ed25519)(\..*)?"
    r"|.*\.(pem|key|pfx|p12|jks|keystore|kdbx|ppk|asc|gpg)|credentials(\..*)?|secrets?(\..*)?|.*_secrets?(\..*)?"
    r"|moodle_state\.json|cookies(\..*)?|login data|local state|key[34]\.db|logins\.json|web data|shadow)$",
    re.IGNORECASE,
)


def is_sensitive(path: "str | os.PathLike[str]") -> bool:
    """Is this a path that holds secrets (a key, a token, a password file, a cookie jar, a browser profile)?"""
    text = os.path.normcase(os.fspath(path)).replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    if not parts:
        return False
    if any(part.lower() in SENSITIVE_FOLDERS for part in parts[:-1]) or parts[-1].lower() in SENSITIVE_FOLDERS:
        return True
    if SENSITIVE_NAMES.match(parts[-1]) and not parts[-1].lower().endswith((".example", ".sample", ".template", ".dist")):
        return True
    joined = "/".join(part.lower() for part in parts)
    return "/mozilla/firefox/" in joined or "/google/chrome/" in joined or "/microsoft/edge/" in joined


def _key(path: "str | os.PathLike[str]") -> str:
    return os.path.normcase(os.path.realpath(path))


class FreeZone:
    def __init__(
        self,
        root: "str | os.PathLike[str] | None" = None,
        reason: str = "",
        protected_files: Iterable[str] = (),
    ):
        self.root: Optional[Path] = Path(os.path.realpath(root)) if root else None
        self.reason = reason  # why there is no zone, when `root` is None
        self._key = _key(self.root) if self.root else ""
        # files at the root of the zone that are protected too (the project instructions file)
        self.protected_files = frozenset(os.path.normcase(name) for name in protected_files)

    @classmethod
    def around(cls, directory: "str | os.PathLike[str] | None", protected_files: Iterable[str] = ()) -> "FreeZone":
        """Zone for `directory`, or an empty zone (with the reason) when it would
        be too wide or is not a real local folder."""
        if not directory:
            return cls(reason="the agent started outside a local folder")
        path = Path(directory)
        if not path.is_dir():
            return cls(reason=f"{directory} is not a local folder")
        resolved = Path(os.path.realpath(path))
        home = Path(os.path.realpath(Path.home()))
        if resolved.parent == resolved:
            return cls(reason="a drive root is too wide to be trusted")
        if resolved == home or resolved in home.parents:
            return cls(reason="your home folder (or one of its parents) is too wide to be trusted")
        return cls(resolved, protected_files=protected_files)

    @property
    def active(self) -> bool:
        return self.root is not None

    def protected(self, path: "str | os.PathLike[str]") -> bool:
        """Is `path` (inside the zone) one that is never free to change?"""
        if self.root is None:
            return False
        relative = os.path.relpath(_key(path), self._key)
        parts = [part for part in relative.split(os.sep) if part not in ("", ".")]
        if not parts:
            return False
        if any(part in PROTECTED_FOLDERS for part in parts[:-1]) or parts[-1] in PROTECTED_FOLDERS:
            return True
        name = parts[-1]
        if name == ".env" or name.startswith(".env."):
            return True
        return len(parts) == 1 and name in self.protected_files

    def contains(self, path: "str | os.PathLike[str]", *, strict: bool = False, write: bool = False) -> bool:
        """Is `path` inside the zone? With `strict`, the zone folder itself does
        not count (removing or moving it is never free); with `write`, neither do
        the protected paths."""
        if self.root is None:
            return False
        target = _key(path)
        if target == self._key:
            return not strict
        if not target.startswith(self._key.rstrip(os.sep) + os.sep):
            return False
        return not (write and self.protected(target))

    def describe(self) -> str:
        return str(self.root) if self.root else f"none ({self.reason})"
