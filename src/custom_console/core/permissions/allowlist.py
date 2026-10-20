"""The read-only allowlist for `local_run`, what an "always" answer may cover, and how a command line is taken apart.

A command is on the list only when it is a *simple command*: no redirection, no substitution, no glob, no variable.
Anything else asks. The list is short on purpose (the plan's own):

    pwd  ls  dir  cat  type  head  tail  wc  grep  which   and   git status | git diff | git log

A command line may chain simple commands with `&&`, `||`, `;` and `|` (and carry `2>&1` or a redirection to the null
device): each part is judged alone, and the line is allowed only when every part is. A part after a pipe may also be a
filter that only reads its input (`head`, `sort`, ...).

An "always" answer is remembered as the exact command, or as a *pattern*: the words that say what runs, then anything
(`uv run pytest:*`). `classify` says which words a pattern must name; a program that runs code by itself (`uv`, `python`,
a shell) can only be covered by a pattern that names the tool it runs, and some are never covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READ_ONLY_COMMANDS = frozenset({"pwd", "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "which"})
PIPE_FILTERS = frozenset({"head", "tail", "wc", "grep", "cat", "type", "sort", "uniq"})
GIT_READ_ONLY = frozenset({"status", "diff", "log"})
GIT_REFUSED_FLAGS = ("--ext-diff", "--output", "--exec-path", "-c", "--config-env", "--git-dir", "--work-tree", "--paginate", "-p")

# Characters with a meaning to a shell. Their presence means "not a simple command".
METACHARACTERS = re.compile(r"[|&;<>`$(){}\[\]*?~!%^#\n\r\x00]")

# These run code or change the system: an "always" answer covers them only through the few patterns in SCRIPT_COMMANDS,
# WRAPPERS, PYTHON_MODULES and TOOLS below, because what they do depends on files that can change after the answer was
# given. Everything else on this list (and every shell, installer, network tool and deleting command) asks every time.
NEVER_REMEMBERED = frozenset({
    "python", "python2", "python3", "py", "pythonw", "pip", "pip3", "pipx", "uv", "uvx", "poetry", "conda", "node", "npm",
    "npx", "yarn", "pnpm", "bun", "deno", "ruby", "gem", "bundle", "perl", "php", "composer", "lua", "java", "javac", "mvn",
    "gradle", "cargo", "rustc", "rustup", "go", "dotnet", "nuget", "make", "cmake", "ninja", "tox", "nox", "ctest",
    "cd", "chdir", "pushd", "popd",  # the rest of the line would run somewhere else
    "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "wsl", "wscript", "cscript", "mshta",
    "rundll32", "regsvr32", "ssh", "scp", "sftp", "curl", "wget", "nc", "ncat", "telnet", "certutil", "bitsadmin", "reg",
    "regedit", "schtasks", "sc", "net", "netsh", "msiexec", "setx", "sudo", "su", "doas", "runas", "chmod", "chown", "icacls",
    "takeown", "eval", "exec", "source", "call", "if", "for", "start", "xargs", "env", "nohup", "timeout", "docker", "podman",
    "kubectl", "terraform", "ansible", "brew", "apt", "apt-get", "dnf", "yum", "pacman", "snap", "choco", "winget", "scoop",
    "systemctl", "service", "crontab", "at", "kill", "killall", "taskkill", "shutdown", "reboot", "format", "mkfs", "dd",
    "rm", "del", "erase", "rd", "rmdir", "mv", "move", "ren", "rename", "cp", "copy", "xcopy", "robocopy", "ln", "mklink",
    "tee", "sed", "awk", "patch", "tar", "unzip", "zip", "7z", "gpg", "openssl", "ssh-keygen", "ssh-add",
})
GIT_NOT_REMEMBERED_NOTE = "git commands other than status, diff and log can run hooks and filters"

# What a pattern must name for the programs above that have a safe, everyday use. (program, first word) -> how many words
# after it say what runs: `npm run lint` is the pattern `npm run lint:*`, `cargo test` is `cargo test:*`.
SCRIPT_COMMANDS: dict[tuple[str, str], int] = {
    **{(manager, "run"): 1 for manager in ("npm", "pnpm", "yarn", "bun")},
    **{(manager, "test"): 0 for manager in ("npm", "pnpm", "yarn", "bun")},
    **{("cargo", sub): 0 for sub in ("test", "check", "clippy", "fmt", "build")},
    **{("go", sub): 0 for sub in ("test", "vet", "build")},
    **{("dotnet", sub): 0 for sub in ("test", "build")},
}
WRAPPERS = frozenset({"uv", "poetry"})  # `uv run TOOL ...`: the pattern names the tool
PYTHON = frozenset({"python", "py", "pythonw"})  # `python -m MODULE ...`: the pattern names the module
# Tools that are not on the never list but are named here so a pattern is offered without asking a model.
TOOLS = frozenset({"pytest", "ruff", "mypy", "black", "isort", "flake8", "pylint", "pyright", "lint-imports", "tsc", "eslint",
                   "prettier", "jest", "vitest"})
PYTHON_MODULES = TOOLS | {"unittest", "coverage", "doctest", "compileall", "pyflakes", "bandit"}
RUNNABLE_SUFFIXES = (".py", ".pyw", ".pyz", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".js", ".mjs", ".cjs", ".rb", ".pl",
                     ".php", ".lua")  # `uv run x.py` runs the file: not a tool name
GENERIC_WHY = "{} runs code or changes the system, and its effect depends on files that can change"


@dataclass(frozen=True, slots=True)
class Simple:
    """A command split into words, or why it could not be."""

    words: tuple[str, ...]
    problem: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.words) and not self.problem

    @property
    def program(self) -> str:
        return program_name(self.words[0]) if self.words else ""


def program_name(word: str) -> str:
    """`C:\\Git\\bin\\git.exe` -> `git`; `./run.sh` -> `run.sh`."""
    base = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def family(program: str) -> str:
    """`python3.12` -> `python`, `pip3` -> `pip`, `node18` -> `node`: a version suffix does not make a different program."""
    return re.sub(r"[-_.]?\d[\d.\-]*$", "", program) or program


def simple_words(command: str, *, windows: bool) -> Simple:
    """The words of a command that has no shell syntax. Quotes group a word; a backslash is an ordinary character on
    Windows and not allowed at all elsewhere (it would be an escape the engine does not model)."""
    text = command.strip()
    if not text:
        return Simple((), "empty command")
    words: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            elif char in "`$\n\r\x00" or (char == "%" and windows) or (char == "\\" and not windows):
                return Simple((), "a substitution or escape inside quotes")
            else:
                current.append(char)
        elif char == '"' or (char == "'" and not windows):  # cmd has no single quotes
            quote = char
            started = True
        elif char in " \t":  # only these separate words; a newline is a command separator (a metacharacter)
            if started or current:
                words.append("".join(current))
                current, started = [], False
        elif METACHARACTERS.search(char):
            return Simple((), f"shell syntax ({char!r})")
        elif char == "\\" and not windows:
            return Simple((), "a backslash escape")
        else:
            current.append(char)
    if quote:
        return Simple((), "an unclosed quote")
    words.append("".join(current))  # the text was stripped, so it ends inside a word or on a closing quote: one is pending
    return Simple(tuple(words))


def is_flag(word: str, program: str, windows: bool) -> bool:
    if word.startswith("-") and len(word) > 1:
        return True
    return bool(windows and program in ("dir", "type", "where") and re.fullmatch(r"/[A-Za-z?](:\S*)?", word))


def path_words(simple: Simple, windows: bool) -> list[str]:
    """The words that may name a path: everything but the program and its flags (and, after `=`, the value of a flag)."""
    out: list[str] = []
    program = simple.program
    for word in simple.words[1:]:
        if is_flag(word, program, windows):
            if "=" in word:
                value = word.split("=", 1)[1]
                if value:
                    out.append(value)
            continue
        out.append(word)
    return out


def is_read_only_command(simple: Simple, windows: bool) -> bool:
    """Whether the command is on the short read-only list (and shaped so that nothing else can run)."""
    if not simple.ok:
        return False
    program = simple.program
    if program == "git":
        words = simple.words
        if len(words) < 2 or words[1] not in GIT_READ_ONLY:
            return False
        return not any(w == f or w.startswith(f + "=") for w in words[2:] for f in GIT_REFUSED_FLAGS)
    return program in READ_ONLY_COMMANDS


PATTERN, OPEN, EXACT, NEVER = "pattern", "open", "exact", "never"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What an "always" answer may cover for one simple command.

    `pattern`: a pattern naming at least `prefix` (the table knows it). `open`: a program the table does not know and the
    never list does not forbid: the exact command, or a pattern a model proposes and `pattern_problem` accepts. `exact`:
    only this exact command. `never`: nothing; `why` says why."""

    kind: str
    prefix: tuple[str, ...] = ()
    why: str = ""

    @property
    def rememberable(self) -> bool:
        return self.kind != NEVER


def _never(why: str) -> Verdict:
    return Verdict(NEVER, why=why)


def classify(simple: Simple, windows: bool) -> Verdict:
    """Which kind of "always" may cover this simple command. Only the words of one command are judged: the caller
    splits a command line first."""
    if not simple.ok:
        return _never(f"it uses {simple.problem}, so what it does cannot be pinned down")
    return _classify(simple.words, windows)


def _classify(words: tuple[str, ...], windows: bool) -> Verdict:
    first, rest = words[0], words[1:]
    if "/" in first or "\\" in first or first.startswith("."):
        return _never("it runs a program by path, and that file can change")
    program = program_name(first)
    fam = family(program)
    if program == "git":
        return Verdict(EXACT) if is_read_only_command(Simple(words), windows) else _never(GIT_NOT_REMEMBERED_NOTE)
    if fam in WRAPPERS and rest[:1] == ("run",):
        inner = rest[1:]
        if not inner or inner[0].startswith("-") or inner[0].lower().endswith(RUNNABLE_SUFFIXES):
            return _never(f"{program} run must be followed by the name of a tool: what it would run cannot be pinned down")
        found = _classify(inner, windows)
        if found.kind == PATTERN:
            return Verdict(PATTERN, (program, "run", *found.prefix))
        if found.kind == OPEN:
            return Verdict(PATTERN, (program, "run", program_name(inner[0])))
        return found
    if fam in PYTHON:
        if rest[:1] == ("-m",) and len(rest) > 1 and rest[1] in PYTHON_MODULES:
            return Verdict(PATTERN, (program, "-m", rest[1]))
        return _never(GENERIC_WHY.format(program))
    if rest and (fam, rest[0]) in SCRIPT_COMMANDS:
        named = rest[1:1 + SCRIPT_COMMANDS[(fam, rest[0])]]
        if len(named) == SCRIPT_COMMANDS[(fam, rest[0])] and not any(n.startswith("-") for n in named):
            return Verdict(PATTERN, (program, rest[0], *named))
        return _never(f"{program} {rest[0]} must name what it runs: it cannot be pinned down")
    if program in TOOLS:
        return Verdict(PATTERN, (program,))
    if program in NEVER_REMEMBERED or fam in NEVER_REMEMBERED:
        return _never(GENERIC_WHY.format(program))
    return Verdict(OPEN)


def can_remember_command(simple: Simple, windows: bool) -> tuple[bool, str]:
    """Whether an "always" answer may cover this exact command, and why not."""
    found = classify(simple, windows)
    return found.rememberable, found.why


def covers(prefix: tuple[str, ...], simple: Simple) -> bool:
    """Whether the command starts with these words (the program by its name, the others as typed)."""
    words = simple.words
    return (simple.ok and 0 < len(prefix) <= len(words) and simple.program == prefix[0]
            and tuple(words[1:len(prefix)]) == tuple(prefix[1:]))


def pattern_problem(prefix: tuple[str, ...], simple: Simple, windows: bool) -> str:
    """Why this pattern may not cover this command (empty: it may). The same check serves a pattern the table, a model or
    the person proposes, and a stored pattern each time it is used."""
    if not covers(prefix, simple):
        return "the pattern is not the start of the command"
    found = classify(simple, windows)
    if found.kind == NEVER:
        return found.why
    if found.kind == EXACT:
        return "this command is remembered only exactly"
    if found.kind == PATTERN and len(prefix) < len(found.prefix):
        return f"too broad: a pattern for this must name at least `{' '.join(found.prefix)}`"
    return ""


def pattern_covers(prefix: tuple[str, ...], simple: Simple, windows: bool) -> bool:
    return not pattern_problem(prefix, simple, windows)


def is_pipe_filter(simple: Simple) -> bool:
    """A program that only reads what it is given and prints: it may follow a pipe without a rule. `sort` and `uniq` can
    write a file, so those forms are left out."""
    if not simple.ok:
        return False
    program, args = simple.program, simple.words[1:]
    if program not in PIPE_FILTERS:
        return False
    if program == "sort":
        return not any(w.startswith(("--output", "--compress-program")) or (w[:1] == "-" and w[1:2] != "-" and "o" in w)
                       for w in args)
    if program == "uniq":
        return sum(1 for w in args if not w.startswith("-")) <= 1
    return True


@dataclass(frozen=True, slots=True)
class Compound:
    """A command line taken apart: its simple commands in order, whether each one reads the output of the one before it,
    or why it could not be."""

    parts: tuple[Simple, ...] = ()
    piped: tuple[bool, ...] = ()
    problem: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.parts) and not self.problem


# `2>&1`, and a redirection to the null device (`/dev/null` where it exists, `nul` on Windows): they write nothing.
_NULL_REDIRECT = {
    False: re.compile(r"(?:[12]?>>?[ \t]*/dev/null|2>&1)(?=[ \t]|$|[|&;])"),
    True: re.compile(r"(?:[12]?>>?[ \t]*nul|2>&1)(?=[ \t]|$|[|&;])", re.I),
}


def split_command(command: str, windows: bool) -> Compound:
    """Split on `&&`, `||`, `;` and `|` outside quotes, drop the harmless redirections, and take each part as a simple
    command. Everything else with a meaning to a shell (a single `&`, `>`, `<`, `$`, a backquote, a glob) makes the line a
    problem, as for a single command. A part nobody wrote (`a &&`, `; b`) is a problem too."""
    text = command.strip()
    if not text:
        return Compound(problem="empty command")
    null = _NULL_REDIRECT[windows]
    pieces: list[tuple[str, bool]] = []
    current: list[str] = []
    quote = ""
    after_pipe = False
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            quote = "" if char == quote else quote
        elif char == '"' or (char == "'" and not windows):
            quote = char
            current.append(char)
        elif text[i:i + 2] in ("&&", "||"):
            pieces.append(("".join(current), after_pipe))
            current, after_pipe = [], False
            i += 1
        elif char in ";|":
            pieces.append(("".join(current), after_pipe))
            current, after_pipe = [], char == "|"
        else:
            skipped = null.match(text, i) if i == 0 or text[i - 1] in " \t" else None
            if skipped:
                i = skipped.end()
                continue
            current.append(char)
        i += 1
    if quote:
        return Compound(problem="an unclosed quote")
    pieces.append(("".join(current), after_pipe))
    parts: list[Simple] = []
    for piece, _ in pieces:
        if not piece.strip():
            return Compound(problem="an empty command next to a separator")
        simple = simple_words(piece, windows=windows)
        if not simple.ok:
            return Compound(problem=simple.problem)
        parts.append(simple)
    return Compound(tuple(parts), tuple(piped for _, piped in pieces))
