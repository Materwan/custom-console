"""The corners of the permission engine, the floor, the rules and the command allowlist that the main tests do not reach.
The plan asks for every branch of these modules to be covered: this file is what is left after the other tests."""

from __future__ import annotations

import json
import sys

import pytest

from custom_console.core.permissions.allowlist import (
    path_words,
    program_name,
    simple_words,
)
from custom_console.core.permissions.engine import ALLOW, ASK, REFUSE, CallInfo, PathUse
from custom_console.core.permissions.floor import FloorConfig, sensitive_reason
from custom_console.core.permissions.rules import RuleKey, RuleStore
from custom_console.core.vfs.backend import BackendFailure, PathRefused, VfsError
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.resolver import Resolver
from custom_console.platform import for_system
from tests.support.memory_backend import MemoryBackend
from tests.unit.test_permissions import World, read, run

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")


class Fussy:
    """A virtual file system that cannot resolve some typed words, to see how the engine takes each kind of failure."""

    def __init__(self, inner: Vfs, refused: dict[str, Exception]):
        self.inner, self.refused = inner, refused

    def resolve(self, typed: str):
        if typed in self.refused:
            raise self.refused[typed]
        return self.inner.resolve(typed)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize(("error", "reason"), [
    (PathRefused("bad: no network paths", "bad"), "bad: no network paths"),
    (BackendFailure("The reMarkable is not set up."), "The reMarkable is not set up."),
    (VfsError("It is gone", "/x/bad"), "/x/bad: It is gone"),
    (VfsError("It is gone"), "path: It is gone"),
])
def test_a_path_the_file_system_cannot_resolve_is_refused_with_its_reason(w, error, reason):
    decision = w.engine.decide(read("bad"), Fussy(w.vfs, {"bad": error}), w.situation)
    assert (decision.effect, decision.step, decision.reason) == (REFUSE, "resolve", reason)


def test_a_word_of_a_command_that_cannot_be_a_path_is_no_reason_to_refuse_it(w):
    decision = w.engine.decide(run("ls weird"), Fussy(w.vfs, {"weird": PathRefused("weird: nope", "weird")}), w.situation)
    assert decision.effect == ALLOW and decision.step == "allowlist"           # `ls` in the folder, and `weird` is just text
    strict = w.engine.decide(read("weird"), Fussy(w.vfs, {"weird": PathRefused("weird: nope", "weird")}), w.situation)
    assert strict.effect == REFUSE                                              # but an argument that must be a path is refused


def test_the_working_folder_of_a_command_that_cannot_be_resolved_is_refused(w):
    decision = w.engine.decide(run("ls", cwd="gone"), Fussy(w.vfs, {"gone": VfsError("No such folder", "gone")}), w.situation)
    assert decision.effect == REFUSE and decision.reason == "gone: No such folder"


def test_a_path_on_another_mount_is_never_inside_the_trusted_folder(tmp_path):
    world = World(tmp_path)
    tablet = MemoryBackend()
    tablet.dirs |= {"/Notes"}
    world.vfs = Vfs(Resolver(for_system("linux"), home=world.home, remarkable=tablet), cwd=str(world.work))
    decision = world.decide(read("reMarkable:Notes/plan"))
    assert decision.effect == ASK and decision.reason == "Outside the trusted folder."


def test_a_decision_knows_whether_it_allows(w):
    assert w.decide(read("notes.txt")).allowed is True
    assert w.decide(read("../../elsewhere/a.txt")).allowed is False


def test_an_answer_to_a_refusal_that_had_no_key_stores_nothing(w):
    plan = w.decide(CallInfo("local_write", "write", (PathUse("x", "write"),)), plan_mode=True)
    assert plan.effect == REFUSE and plan.key is None
    assert w.engine.record(plan, "allow_always") == "" and w.engine.record(plan, "refuse_always") == ""
    assert w.rules.list() == []
    with pytest.raises(ValueError, match="answer must be one of"):
        w.engine.record(plan, "maybe")


# ---------------------------------------------------------------------- the floor
@pytest.mark.parametrize("path", ["", "/", "//"])
def test_a_path_with_no_name_is_not_sensitive(path):
    assert sensitive_reason(path, FloorConfig()) is None


def test_a_browser_profile_folder_is_sensitive_as_a_whole():
    config = FloorConfig(credential_dirs=("/home/me/.mozilla",))
    assert "browser-profile" in sensitive_reason("/home/me/.mozilla/firefox/p/places.sqlite", config)
    assert sensitive_reason("/home/me/.mozilla-notes/readme.md", config) is None


# ---------------------------------------------------------------------- the rules
def test_a_rule_describes_itself_with_its_command_and_paths():
    assert RuleKey("local_read").describe() == "local_read"
    assert RuleKey("local_run", "ls -l").describe() == "local_run 'ls -l'"
    assert RuleKey("local_write", "", ("/a", "/b")).describe() == "local_write on /a, /b"
    assert RuleKey("local_run", "git status", ("/repo",)).describe() == "local_run 'git status' on /repo"


def test_the_rule_store_refuses_a_bad_effect_and_never_stores_the_same_rule_twice(tmp_path):
    store = RuleStore(tmp_path / "rules.json")
    key = RuleKey("local_write", "", ("/a",))
    with pytest.raises(ValueError, match="allow or refuse"):
        store.add("sometimes", key)
    first = store.add("allow", key)
    again = store.add("allow", key)
    assert again.id == first.id and len(store.list()) == 1
    flipped = store.add("refuse", key)                                    # a new answer replaces the opposite one
    assert [r.effect for r in store.list()] == ["refuse"] and flipped.effect == "refuse"
    assert store.match(key, effect="allow") is None
    hit = store.match(key, effect="refuse")
    assert hit is not None and hit.hits == 1 and store.list()[0].hits == 1


def test_the_rule_store_skips_entries_it_cannot_use_and_keeps_the_rest(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [
        "not a rule", {"effect": "maybe", "tool": "local_read"}, {"effect": "allow"}, {"effect": "allow", "tool": ""},
        {"effect": "refuse", "tool": "local_write", "paths": ["/z", "/a"], "hits": 3, "note": "n"}]}))
    rules = RuleStore(path).list()
    assert len(rules) == 1 and rules[0].id == 1 and rules[0].hits == 3 and rules[0].key.paths == ("/a", "/z")
    path.write_text(json.dumps({"rules": "nope"}))
    assert RuleStore(path).list() == []
    assert RuleStore(tmp_path / "missing.json").remove(1) is False


# ---------------------------------------------------------------------- the command allowlist
@pytest.mark.parametrize(("word", "name"), [("C:\\Git\\bin\\git.exe", "git"), ("./run.sh", "run.sh"), ("GIT.COM", "git"),
                                            ("tool.bat", "tool"), ("x.cmd", "x"), ("plain", "plain")])
def test_a_program_is_known_by_its_name_alone(word, name):
    assert program_name(word) == name


@pytest.mark.parametrize(("command", "windows", "words", "problem"), [
    ("ls -l src", False, ("ls", "-l", "src"), ""),
    ('cat "a b.txt"', False, ("cat", "a b.txt"), ""),
    ("cat 'a b.txt'", False, ("cat", "a b.txt"), ""),
    ('echo ""', False, ("echo", ""), ""),                                  # an empty quoted word is a word
    ("ls\tsrc", False, ("ls", "src"), ""),
    ("  ls   src  ", False, ("ls", "src"), ""),                             # runs of blanks and a trailing blank are nothing
    ("", False, (), "empty command"),
    ("   ", False, (), "empty command"),
    ('cat "a', False, (), "an unclosed quote"),
    ('cat "a$HOME"', False, (), "a substitution or escape inside quotes"),
    ('cat "a\\b"', False, (), "a substitution or escape inside quotes"),
    ("cat a\\ b", False, (), "a backslash escape"),
    ("cat a|b", False, (), "shell syntax ('|')"),
    ("cat a*", False, (), "shell syntax ('*')"),
    ('type "%PATH%"', True, (), "a substitution or escape inside quotes"),
    ("type C:\\x\\y.txt", True, ("type", "C:\\x\\y.txt"), ""),
    ("type 'x'", True, ("type", "'x'"), ""),                                # cmd has no single quotes
])
def test_what_counts_as_a_simple_command(command, windows, words, problem):
    found = simple_words(command, windows=windows)
    assert (found.words, found.problem) == (words, problem) and found.ok is (not problem)


def test_the_words_that_may_be_paths_leave_out_the_program_and_its_flags():
    assert path_words(simple_words("grep -n todo notes.txt", windows=False), False) == ["todo", "notes.txt"]
    assert path_words(simple_words("ls --color=auto --sort= src", windows=False), False) == ["auto", "src"]
    assert path_words(simple_words("dir /b C:\\x", windows=True), True) == ["C:\\x"]            # /b is a flag of dir on Windows
    assert path_words(simple_words("dir /a:h C:\\x", windows=True), True) == ["C:\\x"]           # and so is /a:value (no path in it)
    assert path_words(simple_words("pwd", windows=False), False) == []
