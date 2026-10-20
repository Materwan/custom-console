"""A model that proposes the pattern for a command the table does not know (`core/permissions/propose.py` plans when to ask).

One short, incognito Clara conversation with no tools. The model sees only the commands, never a file or an earlier
answer, so nothing it reads can steer it; what it proposes is checked by `pattern_problem` and thrown away if it is
wider than the table allows, so the worst it can do is propose nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Sequence

from ..clara import ClaraClient, ClaraError
from ..clara.events import Done, ErrorEvent, Token, Usage
from ..clara.types import ChatBody
from .ledger import Ledger
from .permissions.allowlist import Simple, pattern_problem, program_name
from .permissions.propose import Pattern
from .session import Session

log = logging.getLogger("custom_console.proposer")

TIMEOUT = 25.0  # seconds: the person is waiting at a prompt
INSTRUCTIONS = (
    "You write permission rules for shell commands. For each command you are given, answer with the start of the command, "
    "counted in whole words from the first word, that names WHAT RUNS, so that the rule later covers the same job with other "
    "arguments.\n"
    "- Keep the program name. Add a subcommand or tool name only when the program does many unrelated jobs "
    "(`flutter test`, `terraform plan`); a program that does one job needs only its name.\n"
    "- Never include flags, file paths, URLs, numbers or other values.\n"
    "Reply with a JSON array of strings, one per command, in the same order, and nothing else."
)


class RuleProposer:
    """Callable as an `Asker`: the commands to cover (as words) -> one pattern each, or nothing if any answer is unusable."""

    def __init__(self, client: ClaraClient, session: Callable[[], Session], ledger: Ledger, model: str, windows: bool):
        self.client, self.session, self.ledger, self.model, self.windows = client, session, ledger, model, windows

    async def __call__(self, commands: Sequence[tuple[str, ...]]) -> Sequence[Pattern]:
        try:
            text = await asyncio.wait_for(self._ask(commands), TIMEOUT)
        except (ClaraError, TimeoutError, OSError) as error:
            log.warning("the pattern model failed: %s", error)
            return ()
        return self.parse(text, commands)

    async def _ask(self, commands: Sequence[tuple[str, ...]]) -> str:
        session = self.session()
        model = self.model or session.model
        body = ChatBody(message=json.dumps([" ".join(words) for words in commands]), instructions=INSTRUCTIONS,
                        model=model, incognito=True, local_only=session.local_only or None)
        reply = ""
        stream = self.client.chat_stream(body)
        async with contextlib.aclosing(stream):
            async for event in stream:
                if isinstance(event, Token):
                    reply += event.text
                elif isinstance(event, Done):
                    reply = event.reply or reply
                elif isinstance(event, Usage):
                    self.ledger.record(session.id, prompt=event.prompt_tokens, completion=event.completion_tokens,
                                       cached=event.cached_tokens, model=model or "", agent="rules")
                elif isinstance(event, ErrorEvent):
                    raise event.as_error()
        return reply

    def parse(self, text: str, commands: Sequence[tuple[str, ...]]) -> Sequence[Pattern]:
        start, end = text.find("["), text.rfind("]")
        try:
            answers = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            answers = None
        if not isinstance(answers, list) or len(answers) != len(commands) or not all(isinstance(a, str) for a in answers):
            return ()
        patterns: list[Pattern] = []
        for words, answer in zip(commands, answers, strict=True):
            named = answer.split()
            if (not 0 < len(named) <= len(words) or named[0].lower() != program_name(words[0])
                    or named[1:] != list(words[1:len(named)])):
                return ()
            pattern = (program_name(words[0]), *words[1:len(named)])
            if pattern_problem(pattern, Simple(words), self.windows):
                return ()
            patterns.append(pattern)
        return tuple(patterns)
