"""The patterns an "always" answer could store for a command: what the prompt offers the person to confirm.

The table in `allowlist.classify` gives the pattern for the commands it knows (`uv run pytest:*`). For a program it does
not know, a model may propose one (`Asker`, set up by the agent); whatever it says is checked by `pattern_problem` before
it is shown, so a model can narrow a rule but never widen it past what the table allows.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .allowlist import EXACT, NEVER, OPEN, PATTERN, Simple, classify

log = logging.getLogger("custom_console.propose")

Pattern = tuple[str, ...]
Asker = Callable[[Sequence[tuple[str, ...]]], Awaitable[Sequence[Pattern]]]  # the commands to cover -> one pattern each


@dataclass(slots=True)
class Proposal:
    """The patterns "always" would keep, known at once or asked for when the person presses the key."""

    patterns: tuple[Pattern, ...] = ()
    ask: Callable[[], Awaitable[tuple[Pattern, ...]]] | None = None

    @property
    def possible(self) -> bool:
        return bool(self.patterns) or self.ask is not None

    async def resolve(self) -> tuple[Pattern, ...]:
        """The patterns, asking the model first if the table did not know the command. Never raises: no answer is no pattern."""
        if not self.patterns and self.ask is not None:
            ask, self.ask = self.ask, None
            try:
                self.patterns = await ask()
            except Exception as error:
                log.warning("no pattern proposed: %s", error)
                self.patterns = ()
        return self.patterns


def plan(parts: Sequence[tuple[str, ...]], windows: bool, asker: Asker | None = None) -> Proposal:
    """The proposal for the parts of a command line that no rule covers. All or nothing: a line with one part that
    cannot be remembered (a shell, `rm`, `git commit`) gets none, because the others would not make it run."""
    known: list[Pattern] = []
    unknown: list[tuple[str, ...]] = []
    for words in parts:
        found = classify(Simple(words), windows)
        if found.kind in (NEVER, EXACT):
            return Proposal()
        if found.kind == PATTERN:
            known.append(found.prefix)
        elif found.kind == OPEN:
            unknown.append(words)
    if not unknown:
        return Proposal(tuple(dict.fromkeys(known)))
    if asker is None:
        return Proposal()
    ask_for = tuple(unknown)

    async def ask() -> tuple[Pattern, ...]:
        proposed = await asker(ask_for)
        if len(proposed) != len(ask_for):
            return ()
        return tuple(dict.fromkeys([*known, *proposed]))

    return Proposal((), ask)
