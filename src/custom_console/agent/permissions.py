"""Permission levels, the gate that asks the user before risky tool calls, and the rules that stop
it from asking again.

A question can be answered:

    y          yes, this once
    a          yes, and for the rest of this session for calls of the same kind (the "rule")
    p          yes, and from now on in this project (saved)
    n [why]    no; what follows is told to the model ("n use pytest -x instead")

A rule is the kind of call an answer covers: a tool (``send_email``), a tool in a folder
(``file_system_write:C:/work/notes``), or for ``run_command`` the program and its subcommand
(``run_command:git status``). A command with shell operators (``&&``, ``|``, ``>``...) gets no rule:
it is always asked, since a harmless start could hide anything after it. So do the programs that run whatever they
are given (``python x.py``, ``node -e``, ``bash``), a flag where the subcommand should be (``git -c ...``), and what
installs or publishes (``pip install``, ``git push``): approving one "for good" would approve what follows.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Set, Union

YES_ANSWERS = ("y", "yes", "o", "oui")
NO_ANSWERS = ("n", "no", "non")
SESSION = "session"
PROJECT = "project"
COMMAND_RULE = "run_command:"


class PermissionLevel(IntEnum):
    """Risk of a tool. A tool is auto-accepted when its level is <= the
    auto-accept level configured for the session."""

    NONE = 0  # harmless (e.g. `todo_write`): accepted without any trace
    READ = 1  # reads / consultation
    WRITE = 2  # writes, deletions, sending, clicking...


LEVEL_LABELS = {
    PermissionLevel.NONE: "always ask",
    PermissionLevel.READ: "reads are auto-accepted",
    PermissionLevel.WRITE: "everything is auto-accepted",
}


def permission_label(level: int) -> str:
    try:
        return LEVEL_LABELS[PermissionLevel(level)]
    except ValueError:
        return f"level {level}"


class UserPermissionDenied(Exception):
    """The user refused an action requested by the agent."""


def is_yes(answer: str) -> bool:
    """For plain yes/no questions: an empty answer means yes."""
    return answer.strip().lower() in ("", *YES_ANSWERS)


@dataclass(frozen=True)
class Decision:
    granted: bool
    remember: str = ""  # SESSION or PROJECT: do not ask again for this rule
    reason: str = ""  # what the user said with a refusal, for the model

    def __bool__(self) -> bool:
        return self.granted


ALLOW = Decision(True)
DENY = Decision(False)


def parse_decision(text: str, *, can_remember: bool = True) -> Optional[Decision]:
    """The decision typed in answer to a tool's question; None for an empty answer (not answered yet).
    Anything that is not a known answer is a refusal, with the text as its reason."""
    text = text.strip()
    if not text:
        return None
    head, _, rest = text.partition(" ")
    word, rest = head.lower().rstrip(",.:;!"), rest.strip()
    if word in YES_ANSWERS and not rest:
        return ALLOW
    if can_remember and word in ("a", "always") and not rest:
        return Decision(True, SESSION)
    if can_remember and word in ("p", "project") and not rest:
        return Decision(True, PROJECT)
    if word in NO_ANSWERS:
        return Decision(False, reason=rest)
    return Decision(False, reason=text)


def _short(value: Any, limit: int) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(name: str, arguments: Mapping[str, Any], value_limit: int = 160) -> str:
    """Human-readable sentence for a tool call (long values are shortened)."""
    action = name.replace("_", " ")
    if not arguments:
        return f"Agent wants to {action}."
    shown = ", ".join(f"{key}={_short(value, value_limit)}" for key, value in arguments.items())
    return f"Agent wants to {action} with {shown}."


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

_SHELL_OPERATORS = re.compile(r"[&|;<>`\n\r]|\$\(|%[A-Za-z_]+%")
_TWO_WORDS = {"git", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "docker", "dotnet", "go", "poetry", "conda"}
# Programs that run whatever they are given (`python x.py`, `node -e ...`, `bash script`): approving one of those "for
# good" would approve everything that follows, so each is asked, except `python -m <module>`
_INTERPRETERS = {
    "python", "python3", "py", "node", "deno", "bun", "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "ruby", "perl",
    "php", "lua", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "msiexec", "wsl", "ssh", "scp", "curl", "wget",
    "iex", "invoke-expression", "start", "start-process", "npx", "bunx", "pipx",
}
# Subcommands that bring in or run somebody else's code, or publish: asked every time
_ALWAYS_ASKED = {"install", "i", "add", "remove", "uninstall", "update", "upgrade", "publish", "exec", "dlx", "x", "run-script", "push", "login"}


def command_rule(command: str) -> Optional[str]:
    """The rule of a shell command: its program, and the subcommand of the usual multi-command tools
    (``git status``, ``python -m pytest``). None when it chains or redirects commands."""
    if _SHELL_OPERATORS.search(command):
        return None
    words = command.split()
    if not words:
        return None
    program = os.path.basename(words[0].strip("\"'")).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if program.endswith(suffix):
            program = program[: -len(suffix)]
    prefix = [program]
    if program in ("python", "python3", "py") and len(words) > 2 and words[1] == "-m":
        prefix += ["-m", words[2].lower()]
    elif program in _INTERPRETERS:
        return None
    elif program in _TWO_WORDS:
        if len(words) < 2 or words[1].startswith("-"):
            return None  # `git -c core.pager=... log`: the subcommand is not where it is expected: asked
        sub = words[1].lower()
        if sub in _ALWAYS_ASKED:
            return None
        prefix.append(sub)
        if sub == "run" and program in ("npm", "pnpm", "yarn") and len(words) > 2:
            prefix.append(words[2].lower())  # which script: `npm run test` is not `npm run anything`
    return COMMAND_RULE + " ".join(prefix)


def rule_label(rule: str) -> str:
    """How a rule is named to the user."""
    if rule.startswith(COMMAND_RULE):
        return f"`{rule[len(COMMAND_RULE):]} …` commands"
    tool, _, what = rule.partition(":")
    if not what:
        return tool
    if "/" in what or "\\" in what:  # a folder
        return f"{tool} in {what}"
    return f"{tool} ({what})"


class ApprovalRules:
    """What the user said to stop asking about: for this session, or for this project (saved in `path`)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.session: Set[str] = set()
        self.project: Set[str] = set()
        self._load()

    @property
    def has_project(self) -> bool:
        return self.path is not None

    def allows(self, rule: Optional[str]) -> str:
        """SESSION or PROJECT when `rule` was approved for good, else ""."""
        if not rule:
            return ""
        if rule in self.project:
            return PROJECT
        return SESSION if rule in self.session else ""

    def add(self, rule: str, scope: str) -> None:
        if scope == PROJECT and self.has_project:
            self.project.add(rule)
            self._save()
        else:
            self.session.add(rule)

    def forget(self) -> None:
        self.session.clear()
        self.project.clear()
        self._save()

    def listed(self) -> List[str]:
        return [f"{rule_label(rule)} (project)" for rule in sorted(self.project)] + [
            f"{rule_label(rule)} (session)" for rule in sorted(self.session - self.project)
        ]

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.project = {str(rule) for rule in data.get("allowed", [])}
        except (OSError, ValueError, AttributeError, TypeError):
            self.project = set()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"allowed": sorted(self.project)}, indent=2), encoding="utf-8")
        except OSError:
            pass  # the rules then only last for this run


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

Ask = Callable[..., Union[bool, Decision]]  # ask(info, rule=None)


@dataclass
class PermissionGate:
    """Decides whether a tool may run.

    `ask(info, rule)` blocks until the user answers (it is called from the agent's worker thread)
    and returns a :class:`Decision` (or a bool); `record` is told about every decision so it can be
    shown.
    """

    auto_level: int
    ask: Ask
    record: Callable[[str, str], None] = lambda info, status: None
    rules: ApprovalRules = field(default_factory=ApprovalRules)

    def request(self, info: str, level: int = PermissionLevel.READ, rule: Optional[str] = None) -> Decision:
        if self.auto_level >= level:
            if level > PermissionLevel.NONE:
                self.record(info, "auto-accepted")
            return ALLOW
        scope = self.rules.allows(rule)
        if scope:
            self.record(info, f"accepted (always, {scope})")
            return ALLOW
        answer = self.ask(info, rule)
        decision = answer if isinstance(answer, Decision) else Decision(bool(answer))
        if decision.granted and decision.remember and rule:
            self.rules.add(rule, decision.remember)
        if decision.granted:
            status = f"accepted, always ({decision.remember})" if decision.remember and rule else "accepted"
        else:
            status = f"refused: {decision.reason}" if decision.reason else "refused"
        self.record(info, status)
        return decision


def refusal_message(tool: str, decision: Decision) -> str:
    message = f"The user refused: {tool}."
    if decision.reason:
        message += f" They said: {decision.reason}"
    return message


def last_given(arguments: Mapping[str, Any], names: Iterable[str]) -> Optional[Any]:
    """The last of `names` that has a value (the destination of a copy, the output of a conversion...)."""
    found = None
    for name in names:
        if arguments.get(name) is not None:
            found = arguments[name]
    return found
