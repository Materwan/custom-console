"""The completion engine: the typed line and the cursor in, candidates out. Plain Python: the prompt only draws them.

    first word    built-ins, aliases, and the programs on the PATH
    flags         each command's own options (read from its definition), with their help
    arguments     what each command says its positionals are: a folder, a file, any path, an app name, an alias, a command
    paths         virtual ones too: `/`, `/C:/`, `/wsl-Ubuntu/`, `/reMarkable/...`, `reMarkable:`, `~` and relative paths;
                  case is ignored where the file system ignores it; folders end with `/`; names with spaces are quoted
    ghost text    the rest of the most recent history line that starts with what is typed

A listing is asked of the backend in the caller's thread with the caller's cancel token, so a new keystroke can drop an old
request. Listings are cached for a few seconds and forgotten when anything is written. A tablet that does not answer in time
gives up with a message.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ...core.vfs.backend import Entry, VfsError
from ...core.vfs.resolver import ROOT, TABLET
from ...platform import Cancelled, CancelToken
from ..base import CommandSpec, Shell
from ..parser import Word, tokenize
from .sources import (
    MAX_CANDIDATES,
    Candidate,
    Completions,
    folder_candidates,
    matches,
    path_executables,
    quote_for_insert,
)

CACHE_SECONDS = 5.0
SLOW_TIMEOUT = 4.0
REDIRECTS = (">", ">>", ">|", "<")
STAGE_BREAKS = ("|", ";", "&")


@dataclass(slots=True)
class Piece:
    start: int
    end: int
    text: str  # without the quotes
    kind: str  # "word" or "op"
    quote: str = ""  # the quote still open at the end of this word


def lex(text: str, windows: bool) -> list[Piece]:
    """Cut a line the way the parser does, but never fail: an open quote just stays open."""
    pieces: list[Piece] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in " \t":
            i += 1
        elif char in "|;&<":
            pieces.append(Piece(i, i + 1, char, "op"))
            i += 1
        elif char == ">":
            width = 2 if text[i + 1:i + 2] in (">", "|") else 1
            pieces.append(Piece(i, i + width, text[i:i + width], "op"))
            i += width
        else:
            start, buf, quote = i, [], ""
            while i < n:
                c = text[i]
                if quote:
                    if c == quote:
                        quote = ""
                    elif c == "\\" and quote == '"' and not windows and text[i + 1:i + 2] in ('"', "\\"):
                        buf.append(text[i + 1])
                        i += 1
                    else:
                        buf.append(c)
                elif c in "'\"":
                    quote = c
                elif c in " \t|;&<>":
                    break
                else:
                    buf.append(c)
                i += 1
            pieces.append(Piece(start, i, "".join(buf), "word", quote))
    return pieces


@dataclass(slots=True)
class _Cached:
    when: float
    generation: int
    entries: list[Entry]


@dataclass
class CompletionEngine:
    shell: Shell
    slow_timeout: float = SLOW_TIMEOUT
    clock: Callable[[], float] = time.monotonic
    slash_commands: Callable[[], Sequence[tuple[str, str]]] = lambda: ()  # the agent's `/commands` and their help
    _cache: dict[str, _Cached] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _programs: list[str] | None = None

    # ------------------------------------------------------------------ public
    def ghost(self, line: str) -> str:
        return str(self.shell.history.ghost(line))

    def complete(self, line: str, cursor: int, cancel: CancelToken | None = None, mode: str = "shell") -> Completions:
        text = line[:cursor]
        windows = self.shell.platform.is_windows
        cancel = cancel or CancelToken()
        if mode == "agent":
            if text.startswith("!"):
                inner = self.complete(text[1:], len(text) - 1, cancel)
                return Completions(inner.start + 1, inner.candidates, inner.message)
            if text.startswith("/") and " " not in text.lstrip():
                return self._slash(text)
            if text.startswith("/"):
                name = text[1:].split(None, 1)[0]
                if self.shell.registry.get(name) is not None:  # `/ls src/`: the shell's own commands, in both modes
                    inner = self.complete(text[1:], len(text) - 1, cancel)
                    return Completions(inner.start + 1, inner.candidates, inner.message)
        pieces = lex(text, windows)
        current = pieces[-1] if pieces and pieces[-1].kind == "word" and pieces[-1].end == len(text) and \
            (pieces[-1].quote or not text.endswith((" ", "\t"))) else None
        if current is None:
            current = Piece(len(text), len(text), "", "word")
            before = pieces
        else:
            before = pieces[:-1]
        stage = self._stage(before)
        redirect = bool(before) and before[-1].kind == "op" and before[-1].text in REDIRECTS
        words = [p for p in stage if p.kind == "word"]
        typed = current.text
        if mode == "agent" and typed.startswith("@") and not redirect:  # an @file mention, anywhere in the message
            result = self._paths(typed[1:], current, "any", cancel, at=True)
        elif redirect:
            result = self._paths(typed, current, "any", cancel)
        elif not words:
            result = self._first_word(typed, current)
        else:
            result = self._argument(words, typed, current, cancel, mode)
        return result if result else self._from_history(line, cursor, result)

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _stage(before: list[Piece]) -> list[Piece]:
        """The pieces of the command being typed: everything after the last pipe."""
        for index in range(len(before) - 1, -1, -1):
            if before[index].kind == "op" and before[index].text in STAGE_BREAKS:
                return before[index + 1:]
        return before

    def _from_history(self, line: str, cursor: int, empty: Completions) -> Completions:
        if cursor != len(line) or not line.strip() or " " not in line:
            return empty
        found = self.shell.history.matching(line, 5)
        return Completions(0, tuple(Candidate(h, h, "history") for h in found if h != line)) if found else empty

    def _slash(self, text: str) -> Completions:
        names = list(self.slash_commands())
        wanted = text[1:]
        extra = [(s.name, s.summary) for s in self.shell.registry.specs()]
        seen: dict[str, str] = {}
        for name, help_text in [*names, *extra]:
            if name.startswith(wanted) and name not in seen:
                seen[name] = help_text
        return Completions(0, tuple(Candidate("/" + n, "/" + n, "command", h) for n, h in sorted(seen.items())))

    def _first_word(self, typed: str, current: Piece) -> Completions:
        if any(c in typed for c in "/\\") or typed.startswith(("~", ".")):
            return Completions(current.start)  # a program given by path: left to path completion by the caller's next Tab
        ci = self.shell.platform.paths.case_insensitive
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for spec in self.shell.registry.specs():
            if matches(spec.name, typed, ci):
                candidates.append(Candidate(spec.name, spec.name, "command", spec.summary))
                seen.add(spec.name)
        for name in self.shell.aliases.names():
            if matches(name, typed, ci) and name not in seen:
                candidates.append(Candidate(name, name, "alias", "alias: " + self.shell.aliases.get(name)[:40]))
                seen.add(name)
        if typed:  # the programs on the PATH, only once something is typed (there are thousands)
            for name in self._path_programs():
                if matches(name, typed, ci) and name not in seen:
                    candidates.append(Candidate(name, name, "command", "program"))
                    seen.add(name)
                    if len(candidates) >= MAX_CANDIDATES:
                        break
        return Completions(current.start, tuple(candidates))

    def _path_programs(self) -> list[str]:
        if self._programs is None:
            self._programs = path_executables(self.shell.env.get("PATH", ""), self.shell.platform.is_windows)
        return self._programs

    # ------------------------------------------------------------------ arguments
    def _effective_command(self, name: str) -> str:
        value = self.shell.aliases.get(name)
        if value:
            first = tokenize(value, windows=self.shell.platform.is_windows)
            if first and isinstance(first[0], Word):
                return first[0].text
        return name

    def _argument(self, words: list[Piece], typed: str, current: Piece, cancel: CancelToken, mode: str) -> Completions:
        name = self._effective_command(words[0].text)
        spec = self.shell.registry.get(name)
        if spec is None:  # an external program: paths and nothing else is known
            return self._paths(typed, current, "any", cancel) if not typed.startswith("-") else Completions(current.start)
        parser = spec.parser()
        options = parser._option_string_actions
        previous = words[-1].text if len(words) > 1 else ""
        taking_value = options.get(previous)
        if taking_value is not None and taking_value.nargs != 0 and previous.startswith("-"):
            return Completions(current.start)  # the value of an option: nothing to suggest
        if typed.startswith("-"):
            ci = self.shell.platform.paths.case_insensitive
            found = [Candidate(flag, flag, "flag", (action.help or "").split("\n")[0][:60])
                     for flag, action in sorted(options.items()) if matches(flag, typed, ci) and flag not in ("-h",)]
            return Completions(current.start, tuple(found))
        index = self._positional_index(words[1:], options)
        kind = spec.kind_at(index)
        if name == "ai" and index == 0:
            return Completions(current.start, (Candidate("agent", "agent", "value", "open the agent console"),)
                               if "agent".startswith(typed) else ())
        return self._by_kind(kind, typed, current, cancel, spec)

    @staticmethod
    def _positional_index(args: list[Piece], options: Mapping[str, object]) -> int:
        count, skip = 0, False
        for piece in args:
            if skip:
                skip = False
            elif piece.text.startswith("-") and piece.text != "-":
                action = options.get(piece.text)
                skip = bool(action is not None and getattr(action, "nargs", 0) != 0)
            else:
                count += 1
        return count

    def _by_kind(self, kind: str, typed: str, current: Piece, cancel: CancelToken, spec: CommandSpec) -> Completions:
        ci = self.shell.platform.paths.case_insensitive
        if kind == "none":
            return Completions(current.start)
        if kind == "command":
            names = sorted({*(s.name for s in self.shell.registry.specs()), *self.shell.aliases.names()})
            return Completions(current.start, tuple(Candidate(n, n, "command") for n in names if matches(n, typed, ci)))
        if kind == "alias":
            return Completions(current.start, tuple(Candidate(n, n, "alias") for n in self.shell.aliases.names() if matches(n, typed, ci)))
        if kind == "app":
            names = sorted({*self.shell.apps.names(), *(self._path_programs() if typed else [])})
            return Completions(current.start, tuple(Candidate(n, n, "app") for n in names if matches(n, typed, ci))[:MAX_CANDIDATES])
        return self._paths(typed, current, "dir" if kind == "dir" else "any", cancel)

    # ------------------------------------------------------------------ paths
    @staticmethod
    def _split(typed: str, windows: bool) -> tuple[str, str]:
        """(the folder part as typed, the name being typed). `reMarkable:No` splits after the colon."""
        seps = "/\\" if windows else "/"
        cut = max((typed.rfind(s) for s in seps), default=-1)
        if cut >= 0:
            return typed[:cut + 1], typed[cut + 1:]
        if typed[:11].lower() == "remarkable:":
            return typed[:11], typed[11:]
        return "", typed

    def _listing(self, typed_folder: str, cancel: CancelToken) -> tuple[list[Entry], bool, str]:
        """(entries, was it the tablet, a message if it gave up). Cached; the cache dies with any change."""
        vfs = self.shell.vfs.fork(cancel)
        resolved = vfs.resolve(typed_folder or ".")
        key = resolved.virtual
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit.generation == self.shell.vfs.generation and self.clock() - hit.when < CACHE_SECONDS:
                return hit.entries, resolved.mount == TABLET, ""
        expired = threading.Event()
        timer = None
        if resolved.mount == TABLET:
            def give_up() -> None:
                expired.set()
                cancel.cancel()
            timer = threading.Timer(self.slow_timeout, give_up)
            timer.daemon = True
            timer.start()
        try:
            info = vfs.stat(resolved)
            if info is None or not info.is_dir:
                return [], resolved.mount == TABLET, ""
            entries = vfs.list(resolved)
        except Cancelled:
            if expired.is_set():
                return [], True, "the reMarkable did not answer in time"
            raise
        except VfsError as error:
            return [], resolved.mount == TABLET, error.message if resolved.mount == TABLET else ""
        finally:
            if timer is not None:
                timer.cancel()
        with self._lock:
            self._cache[key] = _Cached(self.clock(), self.shell.vfs.generation, entries)
        return entries, resolved.mount == TABLET, ""

    def _paths(self, typed: str, current: Piece, kind: str, cancel: CancelToken, at: bool = False) -> Completions:
        windows = self.shell.platform.is_windows
        ci = self.shell.platform.paths.case_insensitive
        folder, prefix = self._split(typed, windows)
        start = current.start + (1 if at else 0)
        sep = "\\" if windows and "\\" in folder and "/" not in folder else "/"
        try:
            entries, _tablet, message = self._listing(folder, cancel)
        except VfsError:
            return Completions(start)
        if message and not entries:
            return Completions(start, message=message)
        wanted = folder_candidates(entries, prefix, kind, ci)
        out: list[Candidate] = []
        for entry in wanted:
            value = folder + entry.name + (sep if entry.is_dir else "")
            text = quote_for_insert(value, current.quote, entry.is_dir, windows)
            out.append(Candidate(text, entry.name + ("/" if entry.is_dir else ""), "dir" if entry.is_dir else "file",
                                 "link" if entry.is_link else ""))
        if not folder:  # the first segment can also be a way into another place
            pseudo = [("~/", "your home folder")]
            if self.shell.remarkable is not None or self.shell.vfs.resolver.remarkable is not None:
                pseudo.append(("reMarkable:", "the tablet"))
            for name, meta in pseudo:
                if matches(name, prefix, ci) and prefix:
                    out.append(Candidate(quote_for_insert(name, current.quote, True, windows), name, "mount", meta))
        if self.shell.vfs.resolve(folder or ".").mount == ROOT and not wanted and not folder:
            return Completions(start)
        return Completions(start, tuple(out))
