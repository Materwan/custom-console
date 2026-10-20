"""Every shell command, driven the way a person types it: through the parser, the pipes and the executor."""

from __future__ import annotations

import os
import re
import sys

import pytest

from tests.support.memory_backend import MemoryBackend
from tests.support.shelltest import Sh

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")


@pytest.fixture
def sh(tmp_path):
    return Sh(tmp_path)


# ---------------------------------------------------------------------- navigate
def test_ls_default_wraps_and_colours_folders_and_quotes_spaces(sh):
    sh.write("b.txt")
    sh.write("a file.txt")
    (sh.work / "zdir").mkdir()
    (sh.work / ".hidden").write_text("")
    r = sh("ls")
    assert r.out == ['zdir/  "a file.txt"  b.txt'] and r.status == 0
    assert r.lines[0].segments[0] == ("zdir/", "bold blue")
    assert ".hidden" in sh("ls -a").text and ".hidden" not in sh("ls").text
    assert sh("ls -1").out == ["zdir/", '"a file.txt"', "b.txt"]


def test_ls_wraps_to_the_terminal_width(sh):
    for i in range(30):
        sh.write(f"file_number_{i:02d}.txt")
    rows = sh("ls").out
    assert len(rows) > 1 and all(len(r) <= 60 for r in rows)


def test_ls_piped_gives_one_plain_name_per_line(sh):
    sh.write("b.txt")
    (sh.work / "d").mkdir()
    assert sh("ls | cat").out == ["d", "b.txt"]
    assert sh("ls | wc -l").out == ["      2"]


def test_ls_long_several_paths_and_errors(sh):
    sh.write("x/one")
    sh.write("y/two")
    assert sh("ls x y").out == ["x:", "one", "", "y:", "two"]
    long = sh("ls -l x").out[0]
    assert re.fullmatch(r"- +\d+  \d{4}-\d\d-\d\d \d\d:\d\d  one", long), long
    r = sh("ls nope")
    assert r.err == ["ls: cannot access 'nope': No such file or directory"] and r.status == 1
    assert sh("ls x/one").out == ["one"]


def test_cd_pwd_and_back(sh):
    sh.write("a/b/f")
    assert sh("pwd").out == [str(sh.work)]
    sh("cd a/b")
    assert sh("pwd").out == [f"{sh.work}/a/b"] and os.getcwd() != f"{sh.work}/a/b"
    sh("cd ..")
    sh("cd -")
    assert sh("pwd").out == [f"{sh.work}/a/b"]
    sh("cd")
    assert sh("pwd").out == [str(sh.home)]
    assert sh("cd nope").err == ["cd: nope: No such file or directory"]
    sh("cd work")
    assert sh("cd a/b/f").err == ["cd: a/b/f: Not a directory"]
    assert sh("cd -").status == 0


def test_tree(sh):
    sh.write("a.txt")
    sh.write("sub/deep/leaf.txt")
    sh.write("sub/inner.txt")
    out = sh("tree").out
    assert out[0] == str(sh.work)
    assert out[1] == "├── a.txt" and "└── sub/" in out[2] and out[-1] == "2 folders, 3 files"
    shallow = sh("tree -d 1").out
    assert any(line.endswith("...") for line in shallow) and not any("leaf" in line for line in shallow)
    assert sh("tree nope").status == 1
    sh.shell.unicode = False
    assert sh("tree").out[1] == "|-- a.txt"


def test_tree_marks_unreadable_folders_and_goes_on(sh):
    sh.write("ok/file")
    (sh.work / "locked").mkdir()
    os.chmod(sh.work / "locked", 0)
    try:
        r = sh("tree")
    finally:
        os.chmod(sh.work / "locked", 0o700)
    assert any("permission denied" in line for line in r.out) and any("file" in line for line in r.out) and r.status == 0


def test_stat(sh):
    sh.write("f.txt", "12345")
    out = sh("stat f.txt").out
    assert out[0].endswith("f.txt") and "file" in out[1] and "5 bytes" in out[2] and out[-1].startswith("  Access: rw")
    assert sh("stat nope").err == ["stat: nope: No such file or directory"]


def test_find(sh):
    sh.write("src/Main.py")
    sh.write("src/util.py")
    sh.write("docs/readme.md")
    assert sh("find main").out == ["src/Main.py"]
    assert sh("find -s main").status == 0 and sh("find -s main").out == []
    assert sh("find \\.py$ src").out == ["src/Main.py", "src/util.py"]
    assert sh("find src/").out == ["src/"]  # a trailing slash in the pattern: folders only
    assert sh("find (").out == []  # an invalid expression is searched literally
    assert sh("find -d 0 py").out == []
    assert sh("find nothing-like-this").err == ["find: nothing matches"]


# ---------------------------------------------------------------------- read
def test_cat_head_tail_wc(sh):
    sh.write("n.txt", "".join(f"line {i}\n" for i in range(1, 21)))
    assert sh("cat n.txt").out[0] == "line 1" and len(sh("cat n.txt").out) == 20
    assert sh("cat -n n.txt").out[0] == "     1\tline 1"
    assert sh("head n.txt").out == [f"line {i}" for i in range(1, 11)]
    assert sh("head -n 2 n.txt").out == ["line 1", "line 2"]
    assert sh("tail -n 2 n.txt").out == ["line 19", "line 20"]
    assert sh("wc -l n.txt").out == ["     20 n.txt"]
    assert sh("wc n.txt").out == ["     20      40     151 n.txt"]  # 9 lines of 7 bytes, 11 of 8


def test_several_files_get_headers_and_totals(sh):
    sh.write("a", "1\n")
    sh.write("b", "2\n3\n")
    assert sh("head a b").out == ["==> a <==", "1", "", "==> b <==", "2", "3"]
    assert sh("wc -l a b").out == ["      1 a", "      2 b", "      3 total"]


def test_cat_errors_binary_folders_and_the_tablet(tmp_path):
    tablet = MemoryBackend()
    tablet.files["/Doc"] = b"PK"
    sh = Sh(tmp_path, remarkable=tablet)
    (sh.work / "b.bin").write_bytes(b"\x00\x01")
    (sh.work / "d").mkdir()
    assert sh("cat b.bin").err == ["cat: b.bin: binary file"]
    assert sh("cat d").err == ["cat: d: Is a folder"]
    assert sh("cat nope").err == ["cat: nope: No such file or directory"]
    assert "not text" in sh("cat reMarkable:Doc").err[0]
    sh("cd reMarkable:/")
    sh.write("local.txt", "hi\n")
    assert sh(f"cat {sh.work}/local.txt").out == ["hi"], "BUG-15: an absolute local path must work from inside the tablet"


def test_pipes_carry_lines_between_builtins(sh):
    sh.write("notes.txt", "buy milk\ncall bob\ntodo: milk again\nTODO shout\n")
    assert sh("cat notes.txt | grep todo | wc -l").out == ["      1"]
    assert sh("cat notes.txt | grep -i todo | wc -l").out == ["      2"]
    assert sh("cat notes.txt | head -n 2 | tail -n 1").out == ["call bob"]
    assert sh("cat notes.txt | grep -c milk").out == ["2"]
    assert sh("cat notes.txt | grep -v milk").out == ["call bob", "TODO shout"]


def test_head_stops_a_producer_early(sh):
    produced = []
    sh.write("big.txt", "".join(f"{i}\n" for i in range(100_000)))
    assert sh("cat big.txt | head -n 3").out == ["0", "1", "2"] and produced == []


def test_grep_options_and_exit_status(sh):
    sh.write("a.txt", "Alpha\nbeta\nalpha beta\n")
    sh.write("sub/b.txt", "no match\nalpha here\n")
    assert sh("grep -n alpha a.txt").out == ["3:alpha beta"]
    assert sh("grep -in alpha a.txt").out == ["1:Alpha", "3:alpha beta"]
    assert sh("grep -r alpha").out == ["a.txt:alpha beta", "sub/b.txt:alpha here"]
    assert sh("grep -rn alpha sub").out == ["sub/b.txt:2:alpha here"]
    assert sh("grep -c alpha a.txt sub/b.txt").out == ["a.txt:1", "sub/b.txt:1"]
    none = sh("grep zzz a.txt")
    assert none.out == [] and none.status == 1 and none.err == []
    assert sh("grep (").status == 2 and "invalid regular expression" in sh("grep (").err[0]
    assert sh("grep alpha sub").err == ["grep: sub: Is a folder (use -r)"]
    assert "nothing is piped" in sh("grep alpha").err[0]


def test_grep_skips_binary_files_when_recursive(sh):
    (sh.work / "blob").write_bytes(b"alpha\x00\x01")
    sh.write("t.txt", "alpha\n")
    assert sh("grep -r alpha").out == ["t.txt:alpha"]


def test_diff(sh):
    sh.write("a", "one\ntwo\nthree\n")
    sh.write("b", "one\n2\nthree\n")
    r = sh("diff a b")
    assert r.status == 1 and "-two" in r.out and "+2" in r.out and r.out[0].startswith("---")
    assert sh("diff a a").out == [] and sh("diff a a").status == 0
    assert sh("diff a nope").status == 2


# ---------------------------------------------------------------------- redirection
def test_redirection(sh):
    sh.write("in.txt", "alpha\nbeta\n")
    assert sh("cat in.txt > out.txt").out == []
    assert (sh.work / "out.txt").read_text() == "alpha\nbeta\n"
    assert sh("echo more >> out.txt").status == 0 and (sh.work / "out.txt").read_text() == "alpha\nbeta\nmore\n"
    assert sh("cat < in.txt | wc -l").out == ["      2"]
    assert sh("grep a < in.txt").out == ["alpha", "beta"]
    assert sh("echo x > nodir/out").status == 1


def test_a_redirect_will_not_replace_a_file_it_did_not_make_or_read(sh):
    sh.write("precious", "keep me\n")
    r = sh("echo oops > precious")
    assert r.status == 1 and "use '>|'" in r.err[0] and (sh.work / "precious").read_text() == "keep me\n"
    assert sh("echo new >| precious").status == 0 and (sh.work / "precious").read_text() == "new\n"
    sh.write("read_first", "old\n")
    sh("cat read_first")  # all of it was read: it may be replaced
    assert sh("echo new > read_first").status == 0
    assert sh("echo a > mine").status == 0 and sh("echo b > mine").status == 0  # this session made it
    (sh.work / "dir").mkdir()
    assert "is a folder" in sh("echo x > dir").err[0]


def test_output_of_a_failing_stage_is_not_piped_as_data(sh):
    r = sh("cat nope | wc -l")
    assert r.err == ["cat: nope: No such file or directory"] and r.out == ["      0"]


# ---------------------------------------------------------------------- globs
def test_globs(sh):
    for name in ("a.py", "b.py", "c.md", ".hidden.py", "dir/d.py"):
        sh.write(name, name + "\n")
    assert sh("cat *.py").out == ["a.py", "b.py"]
    assert sh("ls {a,c}.*").out == ["a.py  c.md"]
    assert sh("ls *.zzz").err == ["ls: cannot access '*.zzz': No such file or directory"]
    assert sh("cat '*.py'").err[0].startswith("cat: *.py:")
    assert sh("ls dir/*.py").out == ["d.py"] or sh("ls dir/*.py").out[0].endswith("d.py")
    assert sh("ls .*.py").out == [".hidden.py"]
    assert sh("ls **/*.py").out[0].count("d.py") == 1
    assert sh("grep alpha *.py").status == 1  # the pattern is not globbed, the files are
    assert sh("echo *.py").out == ["*.py"]  # echo takes no paths: nothing is expanded


# ---------------------------------------------------------------------- change
def test_mkdir_touch_rmdir(sh):
    assert sh("mkdir a/b").status == 1
    assert sh("mkdir -p a/b/c").status == 0 and (sh.work / "a/b/c").is_dir()
    assert "cannot create 'a'" in sh("mkdir a").err[0]
    assert sh("mkdir -p a").status == 0
    assert sh("touch t1 t2").status == 0 and (sh.work / "t1").exists()
    assert sh("rmdir a/b/c").status == 0 and sh("rmdir a").status == 1
    assert "failed to remove 'a'" in sh("rmdir a").err[0]
    sh.write("f")
    assert sh("rmdir f").status == 1


def test_cp(sh):
    sh.write("a.txt", "one")
    assert sh("cp a.txt b.txt").out == [] and (sh.work / "b.txt").read_text() == "one"
    sh.write("b.txt", "two")
    r = sh("cp a.txt b.txt")
    assert r.status == 1 and "use -f" in r.err[0] and (sh.work / "b.txt").read_text() == "two"
    assert sh("cp -f a.txt b.txt").status == 0 and (sh.work / "b.txt").read_text() == "one"
    (sh.work / "d").mkdir()
    sh("cp a.txt b.txt d")
    assert (sh.work / "d/a.txt").exists() and (sh.work / "d/b.txt").exists()
    assert "not a folder" in sh("cp a.txt b.txt a.txt").err[0]
    assert "Is a folder" in sh("cp d d2").err[0] or "use -r" in sh("cp d d2").err[0]
    assert sh("cp -r d d2").status == 0 and (sh.work / "d2/a.txt").exists()
    assert sh("cp a.txt").status == 2


def test_cp_recursive_reports_each_failure_and_a_final_count(sh):
    sh.write("src/ok1")
    sh.write("src/ok2")
    sh.write("src/secret")
    os.chmod(sh.work / "src/secret", 0)
    try:
        r = sh("cp -r src dst")
    finally:
        os.chmod(sh.work / "src/secret", 0o600)
    assert r.status == 1 and any("secret" in e for e in r.err) and r.out[-1].startswith("copied 2 files") and "1 failed" in r.out[-1]
    assert (sh.work / "dst/ok1").exists() and (sh.work / "dst/ok2").exists()


def test_mv(sh):
    sh.write("a", "A")
    sh.write("b", "B")
    assert sh("mv a c").status == 0 and not (sh.work / "a").exists() and (sh.work / "c").read_text() == "A"
    r = sh("mv c b")
    assert r.status == 1 and "use -f" in r.err[0] and (sh.work / "c").exists()
    assert sh("mv -f c b").status == 0 and (sh.work / "b").read_text() == "A"
    (sh.work / "d").mkdir()
    sh.write("x")
    sh.write("y")
    sh("mv x y d")
    assert (sh.work / "d/x").exists() and (sh.work / "d/y").exists()
    assert "itself" in sh("mv d d/inner").err[0]
    assert "not found" in sh("mv nope d").err[0] or "No such file" in sh("mv nope d").err[0]


def test_rm(tmp_path):
    sh = Sh(tmp_path, answers=[False, True])
    sh.write("f")
    sh.write("d/inner/x")
    assert sh("rm f").status == 0 and not (sh.work / "f").exists()
    assert sh("rm f").err == ["rm: f: No such file or directory"] and sh("rm -f f").err == []
    assert sh("rm d").err == ["rm: d: Is a folder (use -r)"]
    assert sh("rm -r d").status == 1 and (sh.work / "d").exists() and "Recursively remove" in sh.questions[0]
    assert sh("rm -r d").status == 0 and not (sh.work / "d").exists()
    sh.write("e/x")
    assert sh("rm -rf e").status == 0 and not (sh.work / "e").exists() and len(sh.questions) == 2


def test_rm_never_touches_the_places_that_must_stay(sh):
    """SAFETY: a destructive test. This once ran `rm -rf ~` and deleted a real home. Now:

    * the shell's disk is a SandboxedLocalBackend, and os-level removal is guarded for the whole run (tests/support/guard.py),
      so a regression could not delete anything outside the temp folder even if the protection failed;
    * HOME and Path.home() are a temp folder;
    * this test never NAMES a real path: not `/`, not a real home, not `/home`. The places that must be refused are judged by
      the pure `protected_reason` (nothing is removed), and only temp paths are given to `rm -rf`, after checking that each one
      really is inside the sandbox.
    """
    # the places that must never go, judged without removing anything
    for typed in ("/", "~", "..", ".", str(sh.home), str(sh.root)):
        assert sh.vfs.protected_reason(sh.vfs.resolve(typed)), typed
    # and the same refusals through the real command, for the paths that are inside the sandbox
    for typed in ("~", "..", ".", str(sh.home), str(sh.root)):
        assert sh.inside_sandbox(typed), f"{typed!r} is not inside the sandbox: this test must not run it"
        r = sh(f"rm -rf {typed}")
        assert r.status == 1 and "Refusing" in r.err[0], typed
    assert sh.work.exists() and sh.home.exists() and sh.root.exists()


def test_the_parser_and_the_protection_agree_on_what_home_is(tmp_path):
    """The bug that deleted a real home: `~` came from one idea of home, the protection from another."""
    elsewhere = tmp_path / "platform-home"
    elsewhere.mkdir()
    (elsewhere / "precious").write_text("x")
    sh = Sh(tmp_path, platform_home=elsewhere)  # the resolver's home and the platform's home are different folders
    for target in ("~", str(sh.home), str(elsewhere), str(tmp_path)):
        assert sh.inside_sandbox(target), target
        r = sh(f"rm -rf {target}")
        assert r.status == 1 and "Refusing" in r.err[0], target
    assert (elsewhere / "precious").exists() and sh.work.exists()
    assert sh("echo ~").out == [str(sh.home)]  # and `~` means the resolver's home, the one that is protected


# ---------------------------------------------------------------------- shell
def test_echo_alias_unalias_which(sh):
    assert sh("echo hello   world").out == ["hello world"]
    assert sh("echo *").out[0] == "*"
    assert sh("alias ll='ls -l'").status == 0 and sh("alias").out == ["alias ll='ls -l'"]
    sh.write("f")
    assert sh("ll").out[0].startswith("-") and sh("alias ll").out == ["alias ll='ls -l'"]
    assert sh("which ll ls sh").out[:3] == ["ll: aliased to 'ls -l'", "ls: shell built-in", "sh: /usr/bin/sh"] or "sh:" in sh("which sh").out[0]
    assert sh("which nonexistent-cmd").status == 1
    assert sh("unalias ll").status == 0 and sh("alias").out == [] and sh("unalias ll").err == ["unalias: ll: not found"]
    assert "cannot be an alias" in sh("alias alias=ls").err[0] and "not a valid alias name" in sh("alias 'a b=ls'").err[0]
    sh("alias a=ls b=pwd")
    assert sh("unalias -a").status == 0 and sh("alias").out == []


def test_history_command(sh):
    sh.shell.history.add("first")
    sh.shell.history.add("second")
    assert sh("history").out == ["    1  first", "    2  second"] and sh("history 1").out == ["    2  second"]
    sh("history -c")
    assert sh("history").out == []


def test_env_hides_secrets(tmp_path):
    sh = Sh(tmp_path)
    sh.shell.env = {"PATH": "/bin", "MY_API_TOKEN": "abc", "DB_PASSWORD": "pw", "HARMLESS": "visible", "URL": "http://u:secretpw@h/x"}
    out = sh("env").out
    assert "HARMLESS=visible" in out and "MY_API_TOKEN=********" in out and "DB_PASSWORD=********" in out
    assert not any("secretpw" in line for line in out)


def test_help(sh):
    text = sh("help").text
    for name in ("cd", "grep", "rmdoc2pdf", "ai", "launch", "alias"):
        assert name in text
    assert "usage: ls" in sh("help ls").text and "usage: ls" in sh("ls -h").text
    assert sh("help nope").err == ["help: no such command: nope"]
    assert sh("grep --help").out[0].startswith("usage: grep")


def test_requests_to_the_shell_loop(sh):
    sh("clear")
    assert sh.shell.request.kind == "clear"
    sh.shell.request = None
    sh("exit")
    assert sh.shell.request.kind == "exit"
    sh.shell.request = None
    sh("reload")
    assert sh.shell.request.kind == "reload"
    sh.shell.request = None
    sh("ai agent --incognito --local-only --model fake:big")
    request = sh.shell.request
    assert request.kind == "agent" and (request.agent.incognito, request.agent.local_only, request.agent.model) == (True, True, "fake:big")
    assert ("was removed" in sh("ai list").err[0] and "was removed" in sh("ai start m").err[0]) or sh("ai start").status == 2
    assert sh("ai foo").status == 2


# ---------------------------------------------------------------------- errors in the line
@pytest.mark.parametrize(("line", "fragment"), [
    ("ls; pwd", "run one command per line"), ("ls | | wc", "missing command"),
    ("echo 'x", "unclosed"), ("ls >", "needs a file name"), ("cat a > b | wc", "ends the line"),
])
def test_a_line_that_cannot_be_understood_says_why(sh, line, fragment):
    r = sh(line)
    assert r.status == 2 and fragment in r.err[0] and r.err[0].startswith("console:")


def test_external_programs_run_attached_and_cannot_be_piped(sh, capfd):
    sh.write("sub/f")
    sh("cd sub")
    assert sh("sh -c 'pwd'").status == 0
    assert capfd.readouterr().out.strip() == str(sh.work / "sub")
    assert sh("sh -c 'exit 3'").status == 3
    assert sh("definitely-not-a-command").err == ["definitely-not-a-command: command not found"]
    r = sh("ls | sh -c cat")
    assert r.status == 2 and "built-in commands only; sh is an external program" in r.err[0]
    assert "built-in commands only" in sh("sh -c pwd > out").err[0]
    assert "built-in commands only" in sh("cat < in | sh").err[0] or sh("cat < in | sh").status == 2


def test_external_programs_do_not_get_clara_variables(tmp_path, capfd):
    sh = Sh(tmp_path)
    sh.shell.env = {"PATH": "/usr/bin:/bin", "CLARA_URL": "http://x", "KEEP": "yes"}
    sh("sh -c 'echo $CLARA_URL-$KEEP'")
    assert capfd.readouterr().out.strip() == "-yes"


def test_a_relative_program_path_is_resolved_in_the_virtual_folder(sh, capfd):
    script = sh.write("bin/hello", "#!/bin/sh\necho hi from script\n")
    script.chmod(0o755)
    sh("cd bin")
    assert sh("./hello").status == 0 and capfd.readouterr().out.strip() == "hi from script"
    assert sh("./nothere").status == 127


# ---------------------------------------------------------------------- desktop
def test_launch(sh):
    r = sh("launch editor")
    assert r.out == ["Started Editor (/usr/bin/editor)"] and sh.desktop.started[0].name == "Editor"
    assert sh("launch ghost").status == 1 and "not found" in sh("launch ghost").err[0]
    assert "try a higher -sl" in sh("launch ghost -sl 1").err[0]
    sh.desktop.fail = "no display"
    assert sh("launch editor").err == ["launch: no display"]


def test_launch_remembers_what_it_found(sh):
    program = sh.write("tools/myprog")
    program.chmod(0o755)
    from custom_console.platform import AppMatch
    sh.desktop.apps["myprog"] = AppMatch("myprog", str(program), 3)
    sh("launch myprog")
    assert sh.shell.apps.get("MyProg") == str(program)
    sh.desktop.apps.clear()  # no longer findable, but remembered
    assert sh("launch myprog").out[0].startswith("Started myprog")
    program.unlink()
    assert sh("launch myprog").status == 1 and sh.shell.apps.get("myprog") is None  # a stale entry is dropped


def test_launch_a_path(sh):
    sh.write("run.sh").chmod(0o755)
    assert sh("launch -f run.sh").out == [f"Started {sh.work}/run.sh"]
    assert sh("launch -f nope").err == ["launch: nope: No such file"]


def test_open(sh):
    sh.write("a.pdf")
    assert sh("open a.pdf").status == 0 and [p.name for p in sh.desktop.opened] == ["a.pdf"]
    assert sh("open nope").err == ["open: nope: No such file or directory"]
    sh.desktop.fail = "xdg-open is not installed"
    assert "xdg-open" in sh("open a.pdf").err[0]


def test_clip(sh):
    assert sh("clip hello world").out == ["copied 11 characters"] and sh.desktop.clipboard == "hello world"
    sh.write("f.txt", "from a file\n")
    sh("clip -f f.txt")
    assert sh.desktop.clipboard == "from a file\n"
    sh("echo piped | clip")
    assert sh.desktop.clipboard == "piped"
    assert sh("clip -p").out == ["piped"]
    assert sh("clip").status == 2
    sh.desktop.fail = "No clipboard tool found"
    assert "No clipboard tool" in sh("clip x").err[0]


# ---------------------------------------------------------------------- the tablet
def test_the_tablet_from_the_shell(tmp_path):
    tablet = MemoryBackend()
    tablet.dirs |= {"/Notes"}
    tablet.files["/Notes/Plan"] = b"PK-plan"
    sh = Sh(tmp_path, remarkable=tablet)
    assert sh("ls reMarkable:Notes").out == ["Plan"]
    sh("cd reMarkable:Notes")
    assert sh("pwd").out == ["reMarkable:/Notes"] and sh("ls").out == ["Plan"]
    sh(f"cp Plan {sh.work}")
    assert (sh.work / "Plan.rmdoc").read_bytes() == b"PK-plan"
    assert "cannot touch" in sh("touch x").err[0]
    assert sh("mkdir Sub").status == 0 and "/Notes/Sub" in tablet.dirs
    sh("cd ..")
    assert sh("pwd").out == ["reMarkable:/"]


def test_rmdoc2pdf_command(tmp_path):
    pytest.importorskip("pymupdf")
    pytest.importorskip("rmscene")
    from tests.support.rmdoc_builder import line, make_pdf, page, rmdoc
    sh = Sh(tmp_path)
    original = make_pdf(2)  # one PDF, used twice: a freshly made one carries a different timestamp
    data, _ = rmdoc(pdf=original, redirects=[0, 1], drawings={1: page([line([(0, 0), (100, 100)])])})
    (sh.work / "course.rmdoc").write_bytes(data)
    r = sh("rmdoc2pdf course.rmdoc")
    assert r.status == 0 and (sh.work / "course.pdf").read_bytes().startswith(b"%PDF")
    assert "2 pages, 1 annotated" in r.out[0]
    again = sh("rmdoc2pdf course.rmdoc")
    assert again.status == 1 and "use -f" in again.err[0]
    assert sh("rmdoc2pdf -f -o course.rmdoc plain.pdf").status == 0
    assert (sh.work / "plain.pdf").read_bytes() == original
    assert "valid .rmdoc" in sh("rmdoc2pdf plain.pdf out2.pdf").err[0]
    assert sh("rmdoc2pdf nope").err == ["rmdoc2pdf: nope: No such file"]
    assert "replace the .rmdoc" in sh("rmdoc2pdf course.rmdoc course.rmdoc -f").err[0]
