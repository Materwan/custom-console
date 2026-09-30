"""Command line tokenizing that copes with Windows paths.

POSIX ``shlex`` treats backslashes as escapes, so ``cd C:\\Users\\me`` would
become ``C:Usersme``. On Windows the line is therefore split in non-POSIX mode
(backslashes are kept) and the surrounding quotes are removed by hand.
"""

from __future__ import annotations

import os
import shlex
from typing import List, Optional

_QUOTES = "'\""


class TokenizeError(ValueError):
    """The line cannot be split (e.g. unbalanced quotes)."""


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in _QUOTES:
        return token[1:-1]
    return token


def split_command(line: str, *, posix: Optional[bool] = None) -> List[str]:
    """Split a command line into tokens.

    `posix` defaults to ``os.name != "nt"``.
    """
    if posix is None:
        posix = os.name != "nt"
    lexer = shlex.shlex(line, posix=posix)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise TokenizeError(str(error)) from error
    return tokens if posix else [_unquote(token) for token in tokens]


def current_word(text: str) -> str:
    """The (raw, possibly quoted) word being typed at the end of `text`."""
    quote: Optional[str] = None
    start = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in _QUOTES:
            quote = char
        elif char.isspace():
            start = index + 1
    return text[start:]


def unquote_word(word: str) -> str:
    """Strip the quotes of a possibly unterminated word (``'My fo`` -> ``My fo``)."""
    if word and word[0] in _QUOTES:
        quote = word[0]
        word = word[1:]
        if word.endswith(quote):
            word = word[:-1]
    return word


def quote_if_needed(text: str) -> str:
    """Quote `text` so it survives :func:`split_command` when it has spaces."""
    if not any(char.isspace() for char in text) and not (text and text[0] in _QUOTES):
        return text
    return f'"{text}"' if "'" in text else f"'{text}'"
