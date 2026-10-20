"""Pattern rules: what a command line is made of, which patterns may cover which commands, and who proposes them.

Pure functions only (no disk, no platform), so these run on every system."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_console.clara import ClaraError
from custom_console.clara.events import Done, ErrorEvent, Token, Usage
from custom_console.core.ledger import Ledger
from custom_console.core.permissions.allowlist import (
    EXACT,
    NEVER,
    OPEN,
    PATTERN,
    classify,
    is_pipe_filter,
    pattern_covers,
    pattern_problem,
    simple_words,
    split_command,
)
from custom_console.core.permissions.propose import Proposal, plan
from custom_console.core.proposer import RuleProposer
from custom_console.core.session import Session


def verdict(command: str, windows: bool = False):
    return classify(simple_words(command, windows=windows), windows)


# ---------------------------------------------------------------------- what a pattern must name
@pytest.mark.parametrize(("command", "prefix"), [
    ("uv run pytest", ("uv", "run", "pytest")),
    ("uv run pytest -q tests", ("uv", "run", "pytest")),
    ("uv run ruff check .", ("uv", "run", "ruff")),
    ("uv run lint-imports", ("uv", "run", "lint-imports")),
    ("uv run mytool --x", ("uv", "run", "mytool")),                       # a tool of the project the table has never heard of
    ("uv run python -m pytest -x", ("uv", "run", "python", "-m", "pytest")),
    ("poetry run mypy", ("poetry", "run", "mypy")),
    ("pytest", ("pytest",)), ("ruff check", ("ruff",)), ("mypy", ("mypy",)), ("flake8 src", ("flake8",)),
    ("python -m pytest", ("python", "-m", "pytest")), ("python3.12 -m mypy src", ("python3.12", "-m", "mypy")),
    ("npm run lint", ("npm", "run", "lint")), ("npm test", ("npm", "test")), ("yarn run build -- --watch", ("yarn", "run", "build")),
    ("cargo test --release", ("cargo", "test")), ("go vet ./...", ("go", "vet")), ("dotnet build", ("dotnet", "build")),
    ("UV.EXE run pytest", ("uv", "run", "pytest")),
])
def test_the_table_says_what_a_pattern_must_name(command, prefix):
    found = verdict(command)
    assert (found.kind, found.prefix) == (PATTERN, prefix), command


@pytest.mark.parametrize("command", [
    "uv", "uv sync", "uv add x", "uv pip install x", "uv run", "uv run -m x", "uv run --with x pytest", "uv run script.py",
    "uv run ./tool", "uv run bash", "uv run sh -c x", "uv run python -c 1", "uv run python x.py", "uv run curl x", "uv run rm x",
    "uvx ruff", "npx x", "pipx run x", "pip install x", "python x.py", "python -c 1", "python -m pip install x",
    "python -m http.server", "python -m", "node x.js", "npm", "npm install", "npm run", "npm --prefix x test", "npm exec x",
    "cargo run", "cargo", "cargo install x", "go run .", "make", "make test", "dotnet run", "bash x", "sh -c x", "powershell x",
    "sudo ls", "ssh host", "curl http://x", "rm x", "mv a b", "cp a b", "tar xf a", "chmod 777 x", "docker run x", "env",
    "cd ..", "pushd x", "./x", "../x", "/bin/ls", "C:\\x\\y.exe", "git commit -m x", "git push", "git -c x=y log",
])
def test_these_can_never_be_covered_by_a_pattern_or_an_exact_answer(command):
    found = verdict(command)
    assert found.kind == NEVER and found.why and not found.rememberable, command


@pytest.mark.parametrize("command", ["git status", "git diff --stat", "git log --oneline"])
def test_read_only_git_is_remembered_exactly_and_never_as_a_pattern(command):
    assert verdict(command).kind == EXACT


@pytest.mark.parametrize("command", ["mytool build", "echo hi", "Remove-Item thing", "terraformish plan"])
def test_a_program_nobody_knows_is_open_to_the_exact_command_or_a_model(command):
    assert verdict(command).kind == OPEN


def test_a_command_that_is_not_simple_is_never():
    found = classify(simple_words("a | b", windows=False), False)
    assert found.kind == NEVER and "shell syntax" in found.why


@pytest.mark.parametrize(("prefix", "command", "problem"), [
    (("uv", "run", "pytest"), "uv run pytest -q", ""),
    (("uv", "run", "pytest", "-q"), "uv run pytest -q tests", ""),              # narrower than the table asks: fine
    (("uv", "run"), "uv run pytest", "too broad"),
    (("uv",), "uv run pytest", "too broad"),
    (("pytest",), "uv run pytest", "not the start"),
    (("uv", "run", "ruff"), "uv run pytest", "not the start"),
    (("sh",), "sh x.sh", "runs code"),
    (("git", "commit"), "git commit -m x", "hooks"),
    (("git", "status"), "git status", "only exactly"),
    (("mytool",), "mytool build", ""),
    (("mytool", "build"), "mytool build", ""),
    ((), "mytool", "not the start"),
])
def test_a_pattern_is_checked_against_the_command_it_is_for(prefix, command, problem):
    found = pattern_problem(prefix, simple_words(command, windows=False), False)
    assert (problem in found) if problem else found == "", (prefix, command, found)


def test_a_stored_pattern_is_checked_again_each_time_it_is_used():
    assert pattern_covers(("uv", "run", "pytest"), simple_words("uv run pytest -q", windows=False), False)
    assert not pattern_covers(("uv", "run", "pytest"), simple_words("uv run pytester", windows=False), False)
    assert not pattern_covers(("uv",), simple_words("uv run pytest", windows=False), False)         # hand-edited too wide
    assert not pattern_covers(("uv", "run", "pytest"), simple_words("./uv run pytest", windows=False), False)  # by path


# ---------------------------------------------------------------------- a command line
def parts(command: str, windows: bool = False):
    line = split_command(command, windows)
    return [p.words for p in line.parts], list(line.piped), line.problem


@pytest.mark.parametrize(("command", "words", "piped"), [
    ("ls", [("ls",)], [False]),
    ("ls -l && pwd", [("ls", "-l"), ("pwd",)], [False, False]),
    ("a || b ; c", [("a",), ("b",), ("c",)], [False, False, False]),
    ("a | b | c", [("a",), ("b",), ("c",)], [False, True, True]),
    ("a | b && c", [("a",), ("b",), ("c",)], [False, True, False]),
    ("uv run ruff check . 2>&1 | head -60", [("uv", "run", "ruff", "check", "."), ("head", "-60")], [False, True]),
    ("pytest 2>/dev/null", [("pytest",)], [False]),
    ("pytest >/dev/null 2>&1", [("pytest",)], [False]),
    ("pytest >> /dev/null", [("pytest",)], [False]),
    ("echo 'a | b && c'", [("echo", "a | b && c")], [False]),
    ('echo "a;b"', [("echo", "a;b")], [False]),
    ("  ls   &&   pwd  ", [("ls",), ("pwd",)], [False, False]),
])
def test_a_command_line_is_taken_apart_on_chaining_and_pipes(command, words, piped):
    assert parts(command) == (words, piped, "")


@pytest.mark.parametrize(("command", "problem"), [
    ("", "empty command"), ("   ", "empty command"),
    ("ls &&", "empty command next to"), ("&& ls", "empty command next to"), ("ls ;; pwd", "empty command next to"),
    ("ls |", "empty command next to"), ("ls | | wc", "empty command next to"),
    ("ls &", "shell syntax"), ("ls & pwd", "shell syntax"), ("ls |& cat", "shell syntax"), ("ls > out", "shell syntax"),
    ("ls >out.txt", "shell syntax"), ("ls 2>&1 >out", "shell syntax"), ("ls 2> err", "shell syntax"),
    ("ls >/dev/nullx", "shell syntax"), ("ls < in", "shell syntax"), ("echo $HOME", "shell syntax"),
    ("echo `x`", "shell syntax"), ("echo $(x)", "shell syntax"), ("ls *", "shell syntax"), ("ls\nrm x", "shell syntax"),
    ("echo 'a", "unclosed quote"), ("ls &&& pwd", "shell syntax"),
])
def test_what_makes_a_whole_line_a_problem(command, problem):
    found, _, why = parts(command)
    assert found == [] and problem in why, (command, why)


def test_the_null_device_depends_on_the_system():
    assert parts("pytest >nul 2>&1", windows=True) == ([("pytest",)], [False], "")
    assert parts("pytest 2>NUL", windows=True) == ([("pytest",)], [False], "")
    assert parts("pytest >nul", windows=False)[2].startswith("shell syntax")        # a file called nul, on Linux
    assert parts("pytest >/dev/null", windows=True)[2].startswith("shell syntax")


def test_a_quote_inside_a_quote_does_not_split_or_end_it():
    assert parts('echo "it\'s | fine"') == ([("echo", "it's | fine")], [False], "")
    assert parts("echo 'a'|wc") == ([("echo", "a"), ("wc",)], [False, True], "")
    assert parts("echo 'a|b'", windows=True) == ([("echo", "'a"), ("b'",)], [False, True], "")  # cmd has no single quotes


@pytest.mark.parametrize(("command", "ok"), [
    ("head -5", True), ("tail -n 3", True), ("wc -l", True), ("grep -n x", True), ("cat", True), ("sort", True),
    ("sort -n -r", True), ("sort -k2", True), ("uniq", True), ("uniq -c", True), ("uniq -f 1", True),
    ("sort -o out", False), ("sort -ro out", False), ("sort --output=out", False), ("sort --compress-program=sh", False),
    ("uniq in out", False), ("tee out", False), ("sed s/a/b/", False), ("awk x", False), ("sh", False), ("xargs rm", False),
])
def test_a_filter_after_a_pipe_only_reads_and_prints(command, ok):
    assert is_pipe_filter(simple_words(command, windows=False)) is ok


# ---------------------------------------------------------------------- who proposes the pattern
def words(command: str) -> tuple[str, ...]:
    return simple_words(command, windows=False).words


async def test_the_table_proposes_without_asking_anyone():
    asked = []

    async def asker(commands):
        asked.append(commands)
        return ()

    proposal = plan([words("uv run pytest -q"), words("uv run pytest tests"), words("ruff check")], False, asker)
    assert proposal.patterns == (("uv", "run", "pytest"), ("ruff",)) and proposal.possible
    assert await proposal.resolve() == proposal.patterns and asked == []


@pytest.mark.parametrize("command", ["sh x.sh", "rm x", "git commit -m x", "git status", "uv run python -c 1", "cd .."])
def test_a_part_that_cannot_be_covered_gives_no_proposal_at_all(command):
    proposal = plan([words("uv run pytest"), words(command)], False)
    assert proposal.patterns == () and proposal.ask is None and not proposal.possible


def test_an_unknown_program_gets_no_proposal_without_a_model():
    assert not plan([words("mytool build")], False).possible


async def test_a_model_is_asked_only_for_the_unknown_parts_and_only_once():
    asked = []

    async def asker(commands):
        asked.append(commands)
        return [("mytool", "build")]

    proposal = plan([words("uv run pytest"), words("mytool build --fast")], False, asker)
    assert proposal.patterns == () and proposal.possible                                  # nothing yet: the key press asks
    assert await proposal.resolve() == (("uv", "run", "pytest"), ("mytool", "build"))
    assert await proposal.resolve() == (("uv", "run", "pytest"), ("mytool", "build")) and asked == [(words("mytool build --fast"),)]


async def test_a_model_that_fails_or_answers_the_wrong_number_means_no_pattern():
    async def wrong_count(commands):
        return []

    async def broken(commands):
        raise RuntimeError("boom")

    assert await plan([words("mytool a")], False, wrong_count).resolve() == ()
    assert await plan([words("mytool a")], False, broken).resolve() == ()
    assert await Proposal().resolve() == ()


# ---------------------------------------------------------------------- the model's answer is only a suggestion
def proposer(client=None, tmp_path=Path("."), model="small"):
    session = Session(folder="/work")
    return RuleProposer(client, lambda: session, Ledger(tmp_path / "usage.jsonl"), model, False)


@pytest.mark.parametrize(("answer", "expected"), [
    ('["mytool build"]', (("mytool", "build"),)),
    ('Here you go: ["mytool"] hope it helps', (("mytool",),)),
    ('["mytool build --fast"]', (("mytool", "build", "--fast"),)),            # narrower than needed is allowed
    ('["mytool"]', (("mytool",),)),
    ('["MYTOOL build"]', (("mytool", "build"),)),                              # the program is known by its lower-case name
    ('["mytool deploy"]', ()), ('["other"]', ()), ('[""]', ()), ("[1]", ()), ('["mytool build now please"]', ()),
    ('["a", "b"]', ()), ("[]", ()), ("no json", ()), ("[unclosed", ()), ('{"a": 1}', ()),
])
def test_the_models_answer_is_parsed_and_checked(answer, expected):
    assert proposer().parse(answer, [words("mytool build --fast")]) == expected


def test_a_model_cannot_widen_a_pattern_past_the_table():
    p = proposer()
    assert p.parse('["uv"]', [words("uv run pytest")]) == ()
    assert p.parse('["sh"]', [words("sh x.sh")]) == ()
    assert p.parse('["git"]', [words("git commit -m x")]) == ()
    assert p.parse('["uv run pytest"]', [words("uv run pytest")]) == (("uv", "run", "pytest"),)


class FakeStream:
    """A stand-in for ClaraClient with one scripted answer to `chat_stream`."""

    def __init__(self, *events, error=None):
        self.events, self.error, self.bodies = events, error, []

    async def chat_stream(self, body):
        self.bodies.append(body)
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


async def test_the_model_is_asked_in_an_incognito_conversation_without_tools(tmp_path):
    client = FakeStream(Token('["myto'), Token('ol build"]'), Usage(11, 4, 2))
    got = await proposer(client, tmp_path)([words("mytool build --fast")])
    assert got == (("mytool", "build"),)
    body = client.bodies[0]
    assert body.incognito is True and body.tools == () and body.conversation is None and body.model == "small"
    assert body.message == '["mytool build --fast"]' and "permission rules" in body.instructions
    assert '"agent":"rules"' in (tmp_path / "usage.jsonl").read_text()


async def test_the_final_reply_wins_over_the_tokens(tmp_path):
    done = Done('["mytool"]', None, (), None, (), "m", False, 0, 0, 0.0, 0, 0)
    assert await proposer(FakeStream(Token("junk"), done), tmp_path)([words("mytool x")]) == (("mytool",),)


async def test_a_failure_of_the_model_is_no_pattern_and_no_exception(tmp_path):
    for client in (FakeStream(error=ClaraError("down", "no", 500)), FakeStream(ErrorEvent(500, "down", "no"))):
        assert await proposer(client, tmp_path)([words("mytool x")]) == ()
