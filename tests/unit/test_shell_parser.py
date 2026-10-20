from __future__ import annotations

import re

import pytest

from custom_console.commands.aliases import Aliases
from custom_console.commands.base import ParseError
from custom_console.commands.history import History
from custom_console.commands.parser import Op, Word, parse, tokenize


def words(line, windows=False, home="/h"):
    return [t.text for t in tokenize(line, windows=windows, home=home)]


@pytest.mark.parametrize(("line", "windows", "expected"), [
    ("ls -la /tmp", False, ["ls", "-la", "/tmp"]),
    ('echo "two words" \'single quoted\'', False, ["echo", "two words", "single quoted"]),
    ("cd C:\\Users\\me", True, ["cd", "C:\\Users\\me"]),
    ("cd C:\\Users\\me", False, ["cd", "C:\\Users\\me"]),  # a backslash is an ordinary character outside quotes
    ('cd "C:\\Program Files\\"', True, ["cd", "C:\\Program Files\\"]),
    ('echo "a \\"quoted\\" word"', False, ["echo", 'a "quoted" word']),
    ("echo a\\b", True, ["echo", "a\\b"]),
    ("echo a'b c'd", False, ["echo", "ab cd"]), ("echo ''", False, ["echo", ""]),
    ("cat a|grep b", False, ["cat", "a", "|", "grep", "b"]), ("echo hi>out.txt", False, ["echo", "hi", ">", "out.txt"]),
    ("echo hi >> out", False, ["echo", "hi", ">>", "out"]), ("echo hi >| out", False, ["echo", "hi", ">|", "out"]),
    ("cat < in", False, ["cat", "<", "in"]), ("echo 'a|b>c'", False, ["echo", "a|b>c"]),
    ("ls # a comment", False, ["ls"]), ("echo a#b", False, ["echo", "a#b"]), ("   ", False, []),
    ("cd ~", False, ["cd", "/h"]), ("cd ~/docs", False, ["cd", "/h/docs"]), ("cd ~\\docs", True, ["cd", "/h\\docs"]),
    ("echo '~' a~b", False, ["echo", "~", "a~b"]),
])
def test_tokens(line, windows, expected):
    assert words(line, windows) == expected


def test_quoting_and_globs_are_recorded():
    tokens = tokenize("ls *.py '*.md' a?b {x,y}", windows=False)
    assert [(t.text, t.quoted, t.glob) for t in tokens] == [("ls", False, False), ("*.py", False, True), ("*.md", True, False),
                                                            ("a?b", False, True), ("{x,y}", False, True)]


@pytest.mark.parametrize(("line", "message"), [
    ("ls; rm x", "';'"), ("a && b", "'&'"), ("a || b", "'||'"), ("sleep 1 &", "'&'"), ("echo `ls`", "'`'"), ("echo $(ls)", "'$('"),
    ("echo 'open", "unclosed '"), ('echo "open', 'unclosed "'),
])
def test_what_is_not_supported_says_so(line, message):
    with pytest.raises(ParseError, match=re.escape(message)):
        tokenize(line, windows=False)


def test_quoted_operators_are_just_text():
    assert words('echo ";" "&&" "$(x)"') == ["echo", ";", "&&", "$(x)"]


def p(line, **kw):
    return parse(line, windows=False, home="/h", **kw)


def test_pipelines_and_redirection():
    pipeline = p("cat < in.txt | grep todo | wc -l >> out.txt")
    assert [[w.text for w in s] for s in pipeline.stages] == [["cat"], ["grep", "todo"], ["wc", "-l"]]
    assert pipeline.stdin.text == "in.txt" and (pipeline.stdout.kind, pipeline.stdout.target.text) == (">>", "out.txt")
    assert p("") is None and p("# only a comment") is None


@pytest.mark.parametrize("line", [
    "| ls", "ls |", "ls | | wc", "ls >", "cat <", "cat a < b < c", "ls > a > b", "ls > a | wc", "ls | cat < in", "> out",
])
def test_bad_pipelines_are_refused_with_a_reason(line):
    with pytest.raises(ParseError):
        p(line)


def test_aliases_are_expanded_once_at_the_start_of_each_command(tmp_path):
    aliases = Aliases(tmp_path / "a.json")
    aliases.set("ll", "ls -l")
    aliases.set("ls", "ls -a")  # an alias for a built-in of the same name must not loop
    aliases.set("count", "grep -c x | wc -l")
    assert [[w.text for w in s] for s in p("ll /tmp", aliases=aliases).stages] == [["ls", "-l", "/tmp"]]
    assert [[w.text for w in s] for s in p("ls", aliases=aliases).stages] == [["ls", "-a"]]
    assert [[w.text for w in s] for s in p("cat f | ll", aliases=aliases).stages] == [["cat", "f"], ["ls", "-l"]]
    assert [[w.text for w in s] for s in p("echo ll", aliases=aliases).stages] == [["echo", "ll"]]  # only the first word
    assert [[w.text for w in s] for s in p('"ll" x', aliases=aliases).stages] == [["ll", "x"]]  # quoted: not an alias
    assert len(p("count f", aliases=aliases).stages) == 2  # an alias may hold a pipe


def test_alias_storage(tmp_path):
    aliases = Aliases(tmp_path / "a.json")
    aliases.set("ll", "ls -l")
    assert Aliases(tmp_path / "a.json").get("ll") == "ls -l"
    for bad in ("", "a b", "x/y"):
        with pytest.raises(ValueError):
            aliases.set(bad or "!", "ls")
    with pytest.raises(ValueError, match="cannot be an alias"):
        aliases.set("alias", "ls")
    with pytest.raises(ValueError, match="needs a value"):
        aliases.set("e", "  ")
    assert aliases.remove("ll") and not aliases.remove("ll") and aliases.names() == []
    (tmp_path / "a.json").write_text("{broken")
    assert Aliases(tmp_path / "a.json").all() == {}


def test_history_rules(tmp_path):
    history = History(tmp_path / "h", cap=5)
    assert history.add("ls -l") and not history.add("ls -l") and not history.add("   ") and not history.add("a\nb")
    assert not history.add("export API_KEY=hunter2hunter2") and not history.add("curl -H 'Authorization: Bearer abcdefghijkl'")
    for i in range(10):
        history.add(f"cmd {i}")
    assert History(tmp_path / "h", cap=5).entries == [f"cmd {i}" for i in range(5, 10)]
    assert history.ghost("cmd ") == "9" and history.ghost("cm") == "d 9" and history.ghost("zzz") == "" and history.ghost("") == ""
    assert history.ghost("cmd 9") == ""  # an exact match leaves nothing to suggest
    assert history.matching("cmd")[:2] == ["cmd 9", "cmd 8"]
    history.clear()
    assert history.entries == [] and not (tmp_path / "h").exists()
    assert isinstance(Word("x"), Word) and isinstance(Op("|"), Op)
