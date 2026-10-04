"""Reading back what the console told the model of a tool call (plain text, ``Error:`` on failure)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_console.agent.results import ERROR_PREFIX


def outcome(text: str) -> SimpleNamespace:
    """`success`, `text` (the whole answer) and `error` (the first line after ``Error:``, or "")."""
    failed = text.startswith(ERROR_PREFIX)
    first, _, rest = text[len(ERROR_PREFIX):].partition("\n") if failed else ("", "", text)
    return SimpleNamespace(success=not failed, text=text, error=first, rest=rest)
