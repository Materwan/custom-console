"""The permission engine: how one local tool call is decided.

    1. Resolve   every path goes through the virtual file system; both the path as written and its real path are checked
    2. Floor     sensitive files, writes under .git, writes outside the trusted folder, network paths: always ask, never
                 an "always" rule
    3. Tainted   after a server tool read outside content, every write and every command asks, whatever the rules say
    4. Allowlist read-only tools inside the trusted folder; for `local_run` a short fixed list of read-only commands
    5. Stored    an "always" answer: the tool, the exact command and all its paths together; never applied to a call that
                 the floor or a tainted turn escalated
    6. Ask       the prompt shows the tool, the exact command or a diff, and every path

A stored *refusal* is checked first of all: saying no is always safe. Plan mode and an untrusted folder refuse before
anything else. The engine decides; it never asks or runs anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...platform import Platform
from ..vfs.backend import BackendFailure, PathRefused, VfsError
from ..vfs.ops import Vfs
from ..vfs.resolver import Resolved
from .allowlist import Simple, can_remember_command, is_read_only_command, path_words, simple_words
from .floor import FloorConfig, PathCheck, floor_reasons, is_inside
from .rules import RuleKey, RuleStore

READ, WRITE, RUN, OPEN, LAUNCH, CLIPBOARD, SAFE = "read", "write", "run", "open", "launch", "clipboard", "safe"
ALLOW, ASK, REFUSE = "allow", "ask", "refuse"
ANSWERS = ("allow_once", "allow_always", "refuse_once", "refuse_always")
ESCALATED_WHEN_TAINTED = frozenset({WRITE, RUN, OPEN, LAUNCH, CLIPBOARD})


@dataclass(frozen=True, slots=True)
class PathUse:
    typed: str
    mode: str = "read"  # "read", "write" or "exec"


@dataclass(frozen=True, slots=True)
class CallInfo:
    """What the engine needs to know about a call; the tool registry builds it from the arguments."""

    tool: str
    kind: str
    paths: tuple[PathUse, ...] = ()
    command: str = ""  # `local_run`: the exact command; `local_launch_app`: the application name
    cwd: str = ""  # `local_run`: where it runs (typed); empty is the agent's folder
    summary: str = ""
    needs_trusted_folder: bool = False  # write, run and git tools: refused in a folder that is not trusted
    remember: bool = True  # False: the answer "always" is never offered (the clipboard)
    undoable: bool = False


@dataclass(frozen=True, slots=True)
class Situation:
    folder: Resolved  # the agent's working folder
    trusted: bool  # whether that folder is trusted
    tainted: bool = False  # a server tool read outside content during this turn
    plan_mode: bool = False


@dataclass(frozen=True, slots=True)
class ShownPath:
    virtual: str
    real: str
    mode: str

    @property
    def redirected(self) -> bool:
        return self.virtual != self.real


@dataclass(frozen=True, slots=True)
class Decision:
    effect: str  # allow, ask or refuse
    step: str  # which step decided: plan, trust, resolve, rule, floor, tainted, allowlist, stored, default
    reason: str
    tool: str = ""
    summary: str = ""
    paths: tuple[ShownPath, ...] = ()
    command: str = ""
    key: RuleKey | None = None
    can_remember: bool = False
    why_not_remembered: str = ""
    floor: tuple[str, ...] = ()
    tainted: bool = False
    extra: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def allowed(self) -> bool:
        return self.effect == ALLOW


class PermissionEngine:
    def __init__(self, platform: Platform, floor_config: FloorConfig, rules: RuleStore):
        self.platform = platform
        self.floor_config = floor_config
        self.rules = rules
        self.windows = platform.is_windows

    # ------------------------------------------------------------------ the decision
    def decide(self, info: CallInfo, vfs: Vfs, situation: Situation) -> Decision:
        def make(effect: str, step: str, reason: str) -> Decision:
            return Decision(effect, step, reason, info.tool, info.summary, tuple(shown), info.command, key, remembers,
                            why_not, tuple(floor), situation.tainted)

        shown: list[ShownPath] = []
        key: RuleKey | None = None
        remembers, why_not = False, ""
        floor: list[str] = []

        if situation.plan_mode and info.kind not in (READ, SAFE):
            return make(REFUSE, "plan", "Plan mode is on: only read tools are available. The person can use /plan off.")
        if info.needs_trusted_folder and not situation.trusted:
            return make(REFUSE, "trust", "This folder is not trusted, so write, run and git tools are not available here.")

        # 1. Resolve --------------------------------------------------------------------------------------
        checks: list[PathCheck] = []
        words: Simple | None = None
        uses = list(info.paths)
        if info.kind == RUN:
            words = simple_words(info.command, windows=self.windows)
        try:
            cwd = vfs.resolve(info.cwd) if info.cwd else situation.folder
            run_uses = [PathUse(w) for w in path_words(words, self.windows)] if words is not None and words.ok else []
            for use in uses:
                self._check(vfs, use, checks, shown, strict=True)
            if info.kind == RUN:
                self._check(vfs, PathUse(cwd.virtual), checks, shown, strict=True)
                for use in run_uses:
                    self._check(vfs, use, checks, shown, strict=False)
        except (PathRefused, BackendFailure) as error:
            return make(REFUSE, "resolve", str(error))
        except VfsError as error:
            return make(REFUSE, "resolve", f"{error.path or 'path'}: {error.message}")

        key = RuleKey(info.tool, info.command if info.kind in (RUN, LAUNCH) else "", self._key_paths(checks))
        if info.kind == RUN:
            assert words is not None
            remembers, why_not = can_remember_command(words, self.windows)
        else:
            remembers = info.remember
            why_not = "" if remembers else "this is asked every time"

        # a stored refusal always applies
        if self.rules.match(key, effect="refuse"):
            return make(REFUSE, "rule", "You told the console never to allow this exact call (see /rules).")

        # 2. Floor ----------------------------------------------------------------------------------------
        trusted = (situation.folder.virtual, situation.folder.real()) if situation.trusted else None
        floor = floor_reasons(checks, trusted, self.floor_config, text=info.command if info.kind == RUN else "")
        if floor:
            remembers, why_not = False, "this touches something that always asks"
            return make(ASK, "floor", "This always asks: " + "; ".join(floor))

        # 3. Tainted turn ---------------------------------------------------------------------------------
        if situation.tainted and info.kind in ESCALATED_WHEN_TAINTED:
            remembers, why_not = False, "this turn read outside content"
            return make(ASK, "tainted", "This turn read outside content (web, GitHub, Drive or an attachment), so "
                                        "every change and every command asks, whatever the rules say.")

        # 4. Allowlist ------------------------------------------------------------------------------------
        if info.kind == SAFE:
            return make(ALLOW, "allowlist", "This tool changes nothing on this computer.")
        inside = situation.trusted and all(self._inside(c, situation.folder) for c in checks)
        if info.kind == READ and inside:
            return make(ALLOW, "allowlist", "A read inside the trusted folder.")
        if info.kind == RUN and inside and words is not None and is_read_only_command(words, self.windows):
            return make(ALLOW, "allowlist", "A read-only command inside the trusted folder.")

        # 5. Stored rules ---------------------------------------------------------------------------------
        if remembers and self.rules.match(key, effect="allow"):
            return make(ALLOW, "stored", "You said \"always\" to exactly this call (see /rules).")

        # 6. Ask ------------------------------------------------------------------------------------------
        reason = "Outside the trusted folder." if info.kind == READ and not inside else "Needs your answer."
        return make(ASK, "default", reason)

    # ------------------------------------------------------------------ pieces
    def _check(self, vfs: Vfs, use: PathUse, checks: list[PathCheck], shown: list[ShownPath], *, strict: bool) -> None:
        try:
            resolved = vfs.resolve(use.typed)
        except (PathRefused, BackendFailure):
            if strict:
                raise
            return  # a word of a command that is not a path
        real = resolved.real()
        checks.append(PathCheck(resolved.virtual, real, use.mode, resolved.mount))
        shown.append(ShownPath(resolved.virtual, real, use.mode))

    def _inside(self, check: PathCheck, folder: Resolved) -> bool:
        if check.mount != folder.mount and not (check.mount in ("posix", "drive") and folder.mount in ("posix", "drive")):
            return False
        ci = self.floor_config.case_insensitive
        return is_inside(check.virtual, folder.virtual, ci) and is_inside(check.real, folder.real(), ci)

    def _key_paths(self, checks: list[PathCheck]) -> tuple[str, ...]:
        from ..vfs.text import fold_path
        return tuple(sorted({fold_path(c.real, self.floor_config.case_insensitive) for c in checks}))

    # ------------------------------------------------------------------ the answer
    def record(self, decision: Decision, answer: str) -> str:
        """Keep an "always" or "never" answer. Returns a line to show ("" when nothing was stored)."""
        if answer not in ANSWERS:
            raise ValueError(f"answer must be one of {', '.join(ANSWERS)}")
        if decision.key is None:
            return ""
        if answer == "allow_always":
            if not decision.can_remember:
                return f"Not remembered: {decision.why_not_remembered or 'this cannot be remembered'}."
            self.rules.add("allow", decision.key)
            return "Remembered for exactly this call."
        if answer == "refuse_always":
            self.rules.add("refuse", decision.key)
            return "Refused from now on for exactly this call."
        return ""
