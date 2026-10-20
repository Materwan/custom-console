"""Turning a typed line into a pipeline: quotes, `~`, pipes, redirection, aliases.

* A backslash is an ordinary character outside quotes, so `cd C:\\Users\\me` works. Inside double quotes it escapes `"` and `\\`
  only where the system is not Windows (on Windows it stays literal, so `"C:\\dir\\"` can end with a backslash).
* `|`, `>`, `>>`, `>|` and `<` are operators outside quotes. Chaining (`;`, `&&`, `||`, `&`) and substitution are not
  supported, and say so, instead of being passed silently to a program as arguments.
* Only the first word of each command is expanded as an alias, once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import ParseError

OPERATORS = ("|", ">>", ">|", ">", "<")
UNSUPPORTED = {
    ";": "';' (chaining commands)", "&": "'&' (chaining or background jobs)", "`": "'`' (command substitution)",
}


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    quoted: bool = False  # some part of it was in quotes
    glob: bool = False  # it has an unquoted `*`, `?`, `[` or `{`


@dataclass(frozen=True, slots=True)
class Op:
    text: str


Token = Word | Op


def tokenize(line: str, *, windows: bool, home: str = "") -> list[Token]:
    tokens: list[Token] = []
    buf: list[str] = []
    started = quoted = glob = tilde = False
    quote = ""
    i, n = 0, len(line)

    def flush() -> None:
        nonlocal buf, started, quoted, glob, tilde
        if started:
            text = "".join(buf)
            if tilde and home and (text == "~" or text[1:2] in ("/", "\\")):
                text = home + text[1:]
            tokens.append(Word(text, quoted, glob))
        buf, started, quoted, glob, tilde = [], False, False, False, False

    while i < n:
        char = line[i]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and not windows and line[i + 1:i + 2] in ('"', "\\"):
                buf.append(line[i + 1])
                i += 1
            else:
                buf.append(char)
        elif char in "'\"":
            quote, started, quoted = char, True, True
        elif char in " \t":
            flush()
        elif char == "#" and not started:
            break
        elif char == "|":
            flush()
            if line[i + 1:i + 2] == "|":
                raise ParseError("'||' is not supported: there is no chaining, and pipes work between built-in commands only")
            tokens.append(Op("|"))
        elif char == ">":
            flush()
            nxt = line[i + 1:i + 2]
            if nxt in (">", "|"):
                tokens.append(Op(">" + nxt))
                i += 1
            else:
                tokens.append(Op(">"))
        elif char == "<":
            flush()
            tokens.append(Op("<"))
        elif char in UNSUPPORTED:
            raise ParseError(f"{UNSUPPORTED[char]} is not supported: run one command per line")
        elif char == "$" and line[i + 1:i + 2] == "(":
            raise ParseError("'$(' (command substitution) is not supported")
        else:
            if char == "~" and not started:
                tilde = True
            if char in "*?[{":
                glob = True
            buf.append(char)
            started = True
        i += 1
    if quote:
        raise ParseError(f"unclosed {quote} quote")
    flush()
    return tokens


def expand_aliases(tokens: list[Token], alias: object, *, windows: bool, home: str) -> list[Token]:
    """The first word of each command, if it is an alias, becomes the words of the alias (once, never again)."""
    get = getattr(alias, "get", None)
    if get is None:
        return tokens
    out: list[Token] = []
    at_start = True
    for token in tokens:
        if isinstance(token, Op):
            out.append(token)
            at_start = token.text == "|"
            continue
        if at_start and not token.quoted:
            value = get(token.text)
            if value:
                out.extend(tokenize(value, windows=windows, home=home))
                at_start = False
                continue
        out.append(token)
        at_start = False
    return out


@dataclass(slots=True)
class Redirect:
    kind: str  # ">", ">>" or ">|"
    target: Word


@dataclass(slots=True)
class Pipeline:
    stages: list[list[Word]] = field(default_factory=list)
    stdin: Word | None = None  # `< file`
    stdout: Redirect | None = None  # `> file`, `>> file`, `>| file`

    @property
    def redirected(self) -> bool:
        return self.stdin is not None or self.stdout is not None


def parse(line: str, *, windows: bool, home: str = "", aliases: object = None) -> Pipeline | None:
    """The pipeline in `line`, or None for an empty line (or a comment)."""
    tokens = expand_aliases(tokenize(line, windows=windows, home=home), aliases, windows=windows, home=home)
    if not tokens:
        return None
    pipeline = Pipeline()
    stage: list[Word] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Word):
            stage.append(token)
        elif token.text == "|":
            if not stage:
                raise ParseError("missing command before '|'")
            pipeline.stages.append(stage)
            stage = []
        else:
            target = tokens[index + 1] if index + 1 < len(tokens) else None
            if not isinstance(target, Word):
                raise ParseError(f"'{token.text}' needs a file name after it")
            if token.text == "<":
                if pipeline.stdin is not None or pipeline.stages:
                    raise ParseError("'<' reads the input of the first command only, once")
                pipeline.stdin = target
            else:
                if pipeline.stdout is not None:
                    raise ParseError("only one output redirection is allowed")
                pipeline.stdout = Redirect(token.text, target)
            index += 1
        index += 1
    if not stage:
        raise ParseError("missing command after '|'" if pipeline.stages else "missing command before the redirection")
    pipeline.stages.append(stage)
    if _redirect_before_pipe(tokens):
        raise ParseError("an output redirection ends the line: put '>' after the last command")
    return pipeline


def _redirect_before_pipe(tokens: list[Token]) -> bool:
    """`cmd > file | other`: the output went to a file, so nothing is left for the pipe."""
    seen = False
    for token in tokens:
        if isinstance(token, Op):
            if token.text in (">", ">>", ">|"):
                seen = True
            elif token.text == "|" and seen:
                return True
    return False
