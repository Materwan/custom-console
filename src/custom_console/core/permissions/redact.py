"""Secrets: finding them in text, hiding them, and keeping them out of a child process's environment.

Redaction runs before anything is written to the journal, a saved session, the history or the log. It is deliberately
greedy: a false positive costs a few characters in a log, a miss leaks a secret.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping

MASK = "[redacted]"

# A name that suggests a secret: a whole segment such as TOKEN or KEY (`GITHUB_TOKEN`, `ssh_key_path`), or camelCase
# (`apiKey`, `authToken`). A word that merely contains one (`monkey`) does not count.
_WORD = r"(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|KEY)"
_BODY = (rf"(?:[A-Za-z0-9]+[_.\-])*(?:(?-i:[a-z0-9]+(?:Key|Token|Secret|Password|Passwd))|{_WORD}s?)"
         r"(?:[_.\-][A-Za-z0-9]+)*")
_NAME = rf"(?<![A-Za-z0-9_.\-]){_BODY}"

_SECRET_KEY = re.compile(rf"(?i)^{_BODY}$")
_BLOCKS = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"clara_[A-Za-z0-9]+_[A-Za-z0-9_\-]{6,}"), MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{8,}=*"), rf"\1 {MASK}"),
    (re.compile(r"(?im)^(\s*(?:proxy-)?authorization\s*[:=]\s*)\S.*$"), rf"\1{MASK}"),
    # NAME=value, NAME: value, --name=value, "name": "value" for names that suggest a secret
    (re.compile(rf"(?i)({_NAME}\s*=\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"';&|]+)"), rf"\1{MASK}"),
    (re.compile(rf"(?i)((?<![A-Za-z0-9])--{_BODY}[= ]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"';&|-][^\s\"';&|]*)"), rf"\1{MASK}"),
    (re.compile(rf"(?i)(\"{_NAME}\"\s*:\s*)\"[^\"\n]*\""), r'\1"' + MASK + '"'),
    (re.compile(rf"(?im)^(\s*{_NAME}\s*:\s*)\S.*$"), rf"\1{MASK}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b"), MASK),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^/\s@]+(@)"), rf"\1{MASK}\2"),  # user:password@host
)

_ENV_PATTERNS = ("*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "*PASSWD*", "*PASSPHRASE*", "*CREDENTIAL*", "CLARA_*")


def redact(text: str) -> str:
    """`text` with the secrets it holds replaced by `[redacted]`."""
    if not text:
        return text
    text = _BLOCKS.sub(f"-----BEGIN PRIVATE KEY-----{MASK}-----END PRIVATE KEY-----", text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_CLARA_TOKEN = re.compile(r"clara_[A-Za-z0-9]+_[A-Za-z0-9_\-]{6,}")


def hide_clara_tokens(text: str) -> str:
    """Only the Clara token pattern. Used on what goes to the model: a file the model is editing must not be rewritten with
    `[redacted]` in it, so the general redaction is for the journal, sessions and logs, not for tool results."""
    return _CLARA_TOKEN.sub(MASK, text)


def looks_secret(text: str) -> bool:
    """Whether a line seems to hold a secret (so the history does not keep it)."""
    return redact(text) != text


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(fnmatch.fnmatchcase(upper, pattern) for pattern in _ENV_PATTERNS)


def scrub_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for a child process: without the token, `CLARA_*` and every variable whose name suggests a secret."""
    return {name: value for name, value in env.items() if not is_secret_name(name)}


def is_secret_key(key: str) -> bool:
    """Whether a dictionary key names a secret (`password`, `API_KEY`, `apiKey`, `auth_token`)."""
    return bool(_SECRET_KEY.match(key))


def redact_value(value: object) -> object:
    """A JSON-like value with every string in it redacted (the shape is kept). The value under a key that names a secret is
    hidden whole, whatever it looks like."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: (MASK if isinstance(key, str) and is_secret_key(key) and isinstance(item, (str, int, float))
                      else redact_value(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
