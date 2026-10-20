"""The floor: what always asks, and can never get an "always" rule.

    sensitive files (.env, the token file, .ssh, credential and browser-profile folders, the console's own data folder),
    writes under .git, writes outside the trusted folder, and network paths other than WSL.

Every path is checked twice, as written and after following links (SEC-4), so a link into a sensitive place does not
slip through.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..vfs.text import fold_path

SENSITIVE_NAMES = frozenset({
    ".env", ".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass", ".my.cnf", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss", "secrets.json", "credentials.json", "service-account.json",
    "login data", "cookies", "key3.db", "key4.db", "logins.json", "kdbx",
})
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".ppk", ".asc", ".gpg")
SENSITIVE_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store"})
UNC_IN_TEXT = re.compile(r"(?<![\w:])(?:\\\\|//)(?![?.]|wsl\$|wsl\.localhost)[\w$.\-]+[\\/][\w$.\-]+", re.I)


@dataclass(frozen=True, slots=True)
class FloorConfig:
    """What the floor needs to know about this machine."""

    credential_dirs: tuple[str, ...] = ()  # browser profiles, key stores (real path strings)
    protected: tuple[str, ...] = ()  # the token file, the data folder, the config folder (real path strings)
    case_insensitive: bool = False


@dataclass(frozen=True, slots=True)
class PathCheck:
    """One path of a call, in the two forms the floor checks."""

    virtual: str  # as resolved, canonical
    real: str  # after following links
    mode: str  # "read", "write" or "exec"
    mount: str = "posix"


def is_inside(path: str, root: str, case_insensitive: bool) -> bool:
    a, b = fold_path(path, case_insensitive).rstrip("/"), fold_path(root, case_insensitive).rstrip("/")
    return a == b or a.startswith(b + "/")


def _components(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def clean_part(part: str) -> str:
    """A name the way Windows reads it: no alternate data stream (`.env::$DATA`), no trailing dots or spaces (`.env. `).
    Applied on every system: a file called `.env ` on Linux is treated as sensitive too, which only ever asks. A part that
    is nothing but dots, spaces and colons (`..`, `:`) stays as it is; a drive `C:` becomes `C`, which no check cares about."""
    return part.split(":", 1)[0].rstrip(" .") or part


def sensitive_reason(path: str, config: FloorConfig) -> str | None:
    """Why this path counts as sensitive, or None."""
    parts = [clean_part(p) for p in _components(path)]
    if not parts:
        return None
    name = parts[-1].lower()
    if name == ".env" or (name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES)):
        return "an environment file (.env)"
    if name in SENSITIVE_NAMES:
        return f"a credential file ({parts[-1]})"
    if name.endswith(SENSITIVE_SUFFIXES):
        return f"a key or certificate file ({parts[-1]})"
    for part in [*parts[:-1], parts[-1]]:
        if part.lower() in SENSITIVE_DIRS:
            return f"inside {part}, which holds credentials"
    if len(parts) >= 2 and parts[-2].lower() == "clara" and name in ("token", "tokens"):
        return "the Clara token file"
    for protected in config.protected:
        if is_inside(path, protected, config.case_insensitive):
            return "the console's own data, which controls its permissions and holds its token"
    for credential in config.credential_dirs:
        if is_inside(path, credential, config.case_insensitive):
            return "a credential or browser-profile folder"
    return None


def under_git(path: str) -> bool:
    return any(part.lower() == ".git" for part in _components(path))


def network_text(text: str) -> bool:
    """Whether a command line names a network path (`\\\\server\\share`), which could send credentials to another host."""
    return bool(UNC_IN_TEXT.search(text))


def floor_reasons(checks: Iterable[PathCheck], trusted: tuple[str, str] | None, config: FloorConfig, *,
                  text: str = "") -> list[str]:
    """Every reason the floor has to ask about these paths (empty: the floor lets it through).

    `trusted` is the trusted folder in its two forms, `(virtual, real)`; each form of a path is compared with the same
    form of the folder (on Windows `/C:/Users/me` and `C:\\Users\\me` are different strings)."""
    reasons: list[str] = []
    for check in checks:
        for form in dict.fromkeys((check.virtual, check.real)):
            found = sensitive_reason(form, config)
            if found:
                reasons.append(f"{check.virtual}: {found}")
                break
        if check.mode == "write":
            if under_git(check.virtual) or under_git(check.real):
                reasons.append(f"{check.virtual}: writes under .git are never automatic")
            if trusted is None or not (is_inside(check.virtual, trusted[0], config.case_insensitive)
                                       and is_inside(check.real, trusted[1], config.case_insensitive)):
                reasons.append(f"{check.virtual}: it is outside the trusted folder")
    if text and network_text(text):
        reasons.append("the command names a network path")
    return list(dict.fromkeys(reasons))

