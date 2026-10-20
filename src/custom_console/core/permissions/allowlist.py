"""The read-only allowlist for `local_run`, and the commands that can never be remembered.

A command is on the list only when it is a *simple command*: no pipe, no redirection, no chaining, no substitution, no
glob, no variable. Anything else asks. The list is short on purpose (the plan's own):

    pwd  ls  dir  cat  type  head  tail  wc  grep  which   and   git status | git diff | git log
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READ_ONLY_COMMANDS = frozenset({"pwd", "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "which"})
GIT_READ_ONLY = frozenset({"status", "diff", "log"})
GIT_REFUSED_FLAGS = ("--ext-diff", "--output", "--exec-path", "-c", "--config-env", "--git-dir", "--work-tree", "--paginate", "-p")

# Characters with a meaning to a shell. Their presence means "not a simple command".
METACHARACTERS = re.compile(r"[|&;<>`$(){}\[\]*?~!%^#\n\r\x00]")

# These run code or change the system: an "always" answer can never cover them, because what they do depends on files
# that can change after the answer was given.
NEVER_REMEMBERED = frozenset({
    "python", "python2", "python3", "py", "pythonw", "pip", "pip3", "pipx", "uv", "uvx", "poetry", "conda", "node", "npm",
    "npx", "yarn", "pnpm", "bun", "deno", "ruby", "gem", "bundle", "perl", "php", "composer", "lua", "java", "javac", "mvn",
    "gradle", "cargo", "rustc", "rustup", "go", "dotnet", "nuget", "make", "cmake", "ninja", "tox", "nox", "pytest", "ctest",
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


def can_remember_command(simple: Simple, windows: bool) -> tuple[bool, str]:
    """Whether an "always" answer may cover this command, and why not."""
    if not simple.ok:
        return False, f"it uses {simple.problem}, so what it does cannot be pinned down"
    program = simple.program
    if program == "git":
        return (True, "") if is_read_only_command(simple, windows) else (False, GIT_NOT_REMEMBERED_NOTE)
    if program in NEVER_REMEMBERED or family(program) in NEVER_REMEMBERED:
        return False, f"{program} runs code or changes the system, and its effect depends on files that can change"
    if "/" in simple.words[0] or "\\" in simple.words[0] or simple.words[0].startswith("."):
        return False, "it runs a program by path, and that file can change"
    return True, ""
