"""The completion engine, without any terminal: a line and a cursor in, candidates out."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from custom_console.commands.completion.engine import CompletionEngine, lex
from custom_console.commands.shell import new_shell
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.resolver import Resolver
from custom_console.paths import DataLayout
from custom_console.platform import Cancelled, CancelToken, Platform
from custom_console.platform.windows import WindowsPaths
from tests.support.harness import FakeDesktop
from tests.support.memory_backend import MemoryBackend
from tests.support.shelltest import Sh

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")


def texts(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.text for c in result.candidates]


# ---------------------------------------------------------------------- the lexer
def test_lex_never_fails_and_keeps_an_open_quote_open():
    pieces = lex('cat "my fi', windows=False)
    assert [(p.text, p.kind, p.quote) for p in pieces] == [("cat", "word", ""), ("my fi", "word", '"')]
    assert [p.text for p in lex("ls | grep a>>out", windows=False)] == ["ls", "|", "grep", "a", ">>", "out"]
    assert lex("   ", windows=False) == []
    assert [(p.start, p.end) for p in lex("ab  cd", windows=False)] == [(0, 2), (4, 6)]


# ---------------------------------------------------------------------- on the local disk
@pytest.fixture
def world(tmp_path):
    sh = Sh(tmp_path)
    for name in ("notes.txt", "numbers.txt", "my file.txt", ".hidden", "src/main.py", "src/util.py", "docs/readme.md", "my dir/in.txt"):
        sh.write(name, "x")
    return sh, CompletionEngine(sh.shell)


@posix_only
def test_first_word_builtins_aliases_and_programs(world):
    sh, engine = world
    sh.shell.aliases.set("ll", "ls -l")
    result = engine.complete("hi", 2)
    assert "history" in texts(result) and result.start == 0
    by_kind = {c.text: c.kind for c in engine.complete("l", 1).candidates}
    assert by_kind["ls"] == "command" and by_kind["ll"] == "alias"
    assert [c.text for c in engine.complete("", 0).candidates] == [s.name for s in sh.shell.registry.specs()] + ["ll"]
    assert any(c.kind == "command" and c.meta == "program" for c in engine.complete("sor", 3).candidates)  # sort(1) on the PATH
    assert engine.complete("zzzz", 4).candidates == ()


def test_flags_come_from_each_commands_own_definition(world):
    _, engine = world
    flags = texts(engine.complete("ls -", 4))
    assert {"-a", "--all", "-l", "--long", "-1"} <= set(flags) and "-h" not in flags
    assert texts(engine.complete("ls --a", 6)) == ["--all"]
    assert {c.meta for c in engine.complete("ls --a", 6).candidates} == {"include hidden entries (names starting with a dot)"}
    assert "--recursive" in texts(engine.complete("grep -", 6)) and "--force" in texts(engine.complete("cp -", 4))
    assert texts(engine.complete("head -n ", 8)) == []  # the value of an option: nothing to suggest
    assert texts(engine.complete("tree -d ", 8)) == []


@posix_only
def test_paths_files_and_folders(world):
    _, engine = world
    assert texts(engine.complete("cat n", 5)) == ["notes.txt", "numbers.txt"]
    assert texts(engine.complete("cd s", 4)) == ["src/"]                        # cd: folders only
    assert texts(engine.complete("cd n", 4)) == []
    assert texts(engine.complete("ls s", 4)) == ["src/"]                        # ls: any path
    assert texts(engine.complete("cat src/", 8)) == ["src/main.py", "src/util.py"]
    assert texts(engine.complete("cat src/m", 9)) == ["src/main.py"]
    assert texts(engine.complete("cat src/../d", 12)) == ["src/../docs/"]
    result = engine.complete("cat n", 5)
    assert result.start == 4 and [c.kind for c in result.candidates] == ["file", "file"]


@posix_only
def test_hidden_names_only_when_the_typed_part_starts_with_a_dot(world):
    _, engine = world
    assert ".hidden" not in texts(engine.complete("ls ", 3))
    assert texts(engine.complete("ls .", 4)) == [".hidden"]


@posix_only
def test_folders_come_first_and_end_with_a_slash(world):
    _, engine = world
    names = [c.display for c in engine.complete("ls ", 3).candidates]
    assert names[:3] == ["docs/", "my dir/", "src/"] and names[3:] == sorted(names[3:])


@posix_only
def test_names_with_spaces_are_quoted_for_you(world):
    _, engine = world
    assert texts(engine.complete("cat my", 6)) == ['"my dir/', '"my file.txt"']  # a file argument also offers folders, to go into
    assert texts(engine.complete('cat "my f', 9)) == ['"my file.txt"']          # a file closes its quote
    assert texts(engine.complete("cd my", 5)) == ['"my dir/']                   # a folder leaves it open to go on
    assert texts(engine.complete('cat "my', 7)) == ['"my dir/', '"my file.txt"']  # inside an open quote the quote is kept
    assert texts(engine.complete('cd "my', 6)) == ['"my dir/']
    assert texts(engine.complete('cat "my dir/', 12)) == ['"my dir/in.txt"']
    assert engine.complete("cat my", 6).start == 4


@posix_only
def test_home_and_the_tablet_are_ways_in(tmp_path):
    tablet = MemoryBackend()
    tablet.dirs |= {"/Notes", "/Notes/Sub"}
    tablet.files["/Notes/Plan"] = b"PK"
    sh = Sh(tmp_path, remarkable=tablet)
    engine = CompletionEngine(sh.shell)
    assert "~/" in texts(engine.complete("cd ~", 4))
    assert texts(engine.complete("cd re", 5)) == ["reMarkable:"]
    assert texts(engine.complete("cd reMarkable:No", 16)) == ["reMarkable:Notes/"]
    assert texts(engine.complete("ls reMarkable:Notes/", 20)) == ["reMarkable:Notes/Sub/", "reMarkable:Notes/Plan"]
    assert texts(engine.complete("cd reMarkable:Notes/", 20)) == ["reMarkable:Notes/Sub/"]
    sh("cd reMarkable:Notes")
    assert texts(engine.complete("cat P", 5)) == ["Plan"]                        # relative to the tablet folder you are in
    assert texts(engine.complete("cat ~/", 6)) == ["~/work/"]                  # `~` is the sandbox home, which holds only `work`


@posix_only
def test_arguments_by_what_each_command_expects(world):
    sh, engine = world
    sh.shell.aliases.set("ll", "ls -l")
    sh.shell.apps.remember("firefox", str(Path(sys.executable)))
    assert texts(engine.complete("grep ", 5)) == []                              # the pattern: nothing to suggest
    assert "notes.txt" in texts(engine.complete("grep todo n", 11))              # then files
    assert texts(engine.complete("find x s", 8)) == ["src/"]                     # find: a pattern, then a folder
    assert texts(engine.complete("launch fire", 11)) == ["firefox"]
    assert "history" in texts(engine.complete("which hi", 8)) and "grep" in texts(engine.complete("help gr", 7))
    assert texts(engine.complete("unalias l", 9)) == ["ll"]
    assert texts(engine.complete("ai a", 4)) == ["agent"] and texts(engine.complete("ai b", 4)) == []
    assert texts(engine.complete("cp -r s", 7)) == ["src/"]                       # flags are skipped when counting arguments
    assert texts(engine.complete("head -n 5 n", 11)) == ["notes.txt", "numbers.txt"]
    assert texts(engine.complete("ll s", 4)) == ["src/"]                         # an alias completes like its command


@posix_only
def test_after_a_pipe_and_a_redirect(world):
    _, engine = world
    assert "grep" in texts(engine.complete("cat notes.txt | gr", 18))
    assert texts(engine.complete("cat notes.txt | grep -", 22))[:1] == ["--count"]
    assert texts(engine.complete("echo hi > n", 11)) == ["notes.txt", "numbers.txt"]
    assert texts(engine.complete("cat < s", 7)) == ["src/"]
    assert texts(engine.complete("cat notes.txt |", 15)) != []  # nothing typed yet after the pipe: the first words


@posix_only
def test_external_programs_get_paths_and_a_history_fallback(world):
    sh, engine = world
    assert texts(engine.complete("git add s", 9)) == ["src/"]
    sh.shell.history.add("git status --short")
    result = engine.complete("git status --", 13)
    assert texts(result) == ["git status --short"] and result.start == 0 and result.candidates[0].kind == "history"
    assert engine.complete("git sta", 7).candidates and engine.ghost("git st") == "atus --short"


def test_ghost_text_is_the_rest_of_the_newest_matching_history_line(world):
    sh, engine = world
    sh.shell.history.add("ls /C:/Users")
    sh.shell.history.add("ls /C:/Windows")
    assert engine.ghost("ls /C") == ":/Windows" and engine.ghost("ls /C:/U") == "sers" and engine.ghost("zzz") == ""


@posix_only
def test_the_cursor_in_the_middle_only_looks_at_what_is_before_it(world):
    _, engine = world
    assert texts(engine.complete("cat n and the rest", 5)) == ["notes.txt", "numbers.txt"]


@posix_only
def test_a_listing_is_cached_and_forgotten_when_anything_is_written(tmp_path):
    sh = Sh(tmp_path)
    sh.write("a1")
    calls = []
    backend = sh.vfs.resolver._local
    original = backend.list
    backend.list = lambda inner, cancel=None: (calls.append(inner), original(inner, cancel))[1]  # type: ignore[method-assign]
    engine = CompletionEngine(sh.shell)
    engine.complete("cat a", 5)
    engine.complete("cat a", 5)
    assert len(calls) == 1
    sh("touch a2")                                                     # a write: the generation moves
    assert texts(engine.complete("cat a", 5)) == ["a1", "a2"] and len(calls) == 2


# ---------------------------------------------------------------------- the agent's extras
@posix_only
def test_agent_mode_slash_bang_and_at(world):
    sh, _ = world
    engine = CompletionEngine(sh.shell, slash_commands=lambda: [("model", "choose the model"), ("compact", "compact the chat")])
    names = texts(engine.complete("/mo", 3, mode="agent"))
    assert names == ["/model"] and "/compact" in texts(engine.complete("/co", 3, mode="agent"))
    assert "/ls" in texts(engine.complete("/l", 2, mode="agent"))            # the shell's commands exist as slash commands too
    assert texts(engine.complete("/cat n", 6, mode="agent")) == ["notes.txt", "numbers.txt"]
    result = engine.complete("!cat n", 6, mode="agent")
    assert texts(result) == ["notes.txt", "numbers.txt"] and result.start == 5
    mention = engine.complete("look at @src/m", 14, mode="agent")
    assert texts(mention) == ["src/main.py"] and mention.start == 9           # the @ stays, the path is completed
    assert engine.complete("@s", 2, mode="agent").candidates


# ---------------------------------------------------------------------- slow and cancelled
class HangingTablet(MemoryBackend):
    """A tablet that never answers until it is cancelled."""

    def list(self, inner, cancel=None):  # type: ignore[no-untyped-def]
        while cancel is None or not cancel.cancelled:
            time.sleep(0.02)
        cancel.raise_if_cancelled()  # type: ignore[union-attr]
        return []


@posix_only
def test_a_tablet_that_does_not_answer_gives_up_with_a_message(tmp_path):
    sh = Sh(tmp_path, remarkable=HangingTablet())
    engine = CompletionEngine(sh.shell, slow_timeout=0.3)
    begun = time.monotonic()
    result = engine.complete("ls reMarkable:No", 16)
    assert time.monotonic() - begun < 3 and result.candidates == () and "did not answer in time" in result.message


@posix_only
def test_a_new_keystroke_cancels_the_old_request(tmp_path):
    sh = Sh(tmp_path, remarkable=HangingTablet())
    engine = CompletionEngine(sh.shell, slow_timeout=30)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    begun = time.monotonic()
    with pytest.raises(Cancelled):
        engine.complete("ls reMarkable:No", 16, token)
    assert time.monotonic() - begun < 3


# ---------------------------------------------------------------------- Windows forms, without Windows
class FakeWindowsPaths(WindowsPaths):
    def home(self) -> Path:
        return Path("C:/Users/me")

    def drives(self) -> list[str]:
        return ["C", "D"]

    def wsl_distros(self) -> list[str]:
        return ["Ubuntu"]


class DriveMemory(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.dirs |= {"C:", "C:/Users", "C:/Users/me", "C:/Users/me/Documents", "C:/Program Files", "D:"}
        self.files["C:/Users/me/notes.txt"] = b"x"


class WslMemory(MemoryBackend):
    def _t(self, inner: str) -> str:
        parts = [p for p in inner.replace("\\", "/").split("/") if p]
        return "/" + "/".join(parts[2:])

    def list(self, inner, cancel=None):  # type: ignore[no-untyped-def]
        return super().list(self._t(inner), cancel)

    def stat(self, inner):  # type: ignore[no-untyped-def]
        return super().stat(self._t(inner))


@pytest.fixture
def windows_world(tmp_path):
    from custom_console.platform import for_system
    base = for_system("linux")
    platform = Platform("windows", base.privacy, FakeDesktop(), FakeWindowsPaths())
    resolver = Resolver(platform, home=Path("C:/Users/me"), local=DriveMemory())
    wsl = WslMemory()
    wsl.dirs |= {"/home", "/home/me"}
    resolver._wsl["Ubuntu"] = wsl  # type: ignore[assignment]
    vfs = Vfs(resolver, cwd="/C:/Users/me")
    shell = new_shell(vfs, platform, DataLayout(tmp_path / "data"))
    return CompletionEngine(shell), shell


def test_windows_drives_wsl_and_the_tablet_at_the_root(windows_world):
    engine, _ = windows_world
    assert texts(engine.complete("cd /", 4)) == ["/C:/", "/D:/", "/reMarkable/", "/wsl-Ubuntu/"]
    assert texts(engine.complete("cd /C", 5)) == ["/C:/"]
    assert texts(engine.complete("cd /wsl-U", 9)) == ["/wsl-Ubuntu/"]
    assert texts(engine.complete("cd /wsl-Ubuntu/h", 16)) == ["/wsl-Ubuntu/home/"]
    assert texts(engine.complete("cd /wsl-Ubuntu/home/", 20)) == ["/wsl-Ubuntu/home/me/"]


def test_windows_paths_with_backslashes_keep_their_separator_and_ignore_case(windows_world):
    engine, _ = windows_world
    assert texts(engine.complete("cd C:\\us", 8)) == ["C:\\Users\\"]
    assert texts(engine.complete("cd c:/us", 8)) == ["c:/Users/"]
    assert texts(engine.complete("cat C:\\Users\\me\\n", 17)) == ["C:\\Users\\me\\notes.txt"]
    assert texts(engine.complete('cd "C:\\Program', 15)) == ['"C:\\Program Files\\']
    assert texts(engine.complete("cd Doc", 6)) == ["Documents/"]                   # relative to /C:/Users/me, case ignored
    assert texts(engine.complete("cd doc", 6)) == ["Documents/"]
    assert texts(engine.complete("cd ~\\", 5)) == ["~\\Documents\\"]
    assert "~/" in texts(engine.complete("cd ~", 4))

