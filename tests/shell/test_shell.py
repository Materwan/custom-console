"""End-to-end tests of command execution through Shell.execute()."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from custom_console.fs import FileManager
from custom_console.settings import load_settings
from custom_console.shell import Shell
from custom_console.shell.commands import build_registry
from custom_console.shell.printer import Printer
from custom_console.shell.repl import format_error


class Harness:
    def __init__(self, root: Path, answer: bool = True):
        self.buffer = io.StringIO()
        self.asked = []

        def confirm(question: str) -> bool:
            self.asked.append(question)
            return answer

        settings = load_settings({}, root=root, use_dotenv=False)
        self.shell = Shell(
            settings,
            printer=Printer(Console(file=self.buffer, force_terminal=False, width=120)),
            files=FileManager(start_dir=str(root)),
            confirm=confirm,
        )

    def run(self, line: str) -> str:
        self.buffer.seek(0)
        self.buffer.truncate()
        self.shell.execute(line)
        return self.buffer.getvalue()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "a b.txt").write_text("hello\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "n.txt").write_text("x")
    (tmp_path / "markup.txt").write_text("[red]not markup[/red] [/bad]")
    return tmp_path


@pytest.fixture
def h(root):
    return Harness(root)


class TestCommands:
    def test_ls_quotes_names_with_spaces_and_marks_directories(self, h):
        out = h.run("ls")
        assert "'a b.txt'" in out and "sub/" in out

    def test_ls_hidden(self, h, root):
        (root / ".secret").write_text("x")
        assert ".secret" not in h.run("ls")
        assert ".secret" in h.run("ls -a")

    def test_cat_prints_content_verbatim_without_extra_blank_line(self, h):
        assert h.run("cat 'a b.txt'") == "hello\n"

    def test_cat_does_not_interpret_markup(self, h):
        assert "[red]not markup[/red] [/bad]" in h.run("cat markup.txt")

    def test_echo_does_not_interpret_markup(self, h):
        assert h.run("echo [red]hi[/red]").strip() == "[red]hi[/red]"

    def test_cd_and_pwd(self, h, root):
        h.run("cd sub")
        assert h.run("pwd").strip().endswith("/sub")
        h.run("cd ..")
        assert h.run("pwd").strip() == str(root).replace("\\", "/")

    def test_stat_shows_the_requested_path(self, h):
        out = h.run("stat sub")
        assert out.startswith("sub ") and "r" in out

    def test_find(self, h):
        assert "sub/n.txt" in h.run("find n.txt")
        assert "No match" in h.run("find zzzz")

    def test_tree(self, h):
        out = h.run("tree")
        assert "sub/" in out and "n.txt" in out

    def test_cp_and_rm(self, h, root):
        assert "1 file(s) copied" in h.run("cp 'a b.txt' c.txt")
        assert (root / "c.txt").read_text() == "hello\n"
        h.run("rm c.txt")
        assert not (root / "c.txt").exists()

    def test_rm_recursive_asks_for_confirmation(self, root):
        h = Harness(root, answer=False)
        h.run("rm -r sub")
        assert (root / "sub").exists() and h.asked

        h = Harness(root, answer=True)
        h.run("rm -r sub")
        assert not (root / "sub").exists()

    def test_rm_recursive_force_skips_confirmation(self, h, root):
        h.run("rm -rf sub")
        assert not (root / "sub").exists() and not h.asked

    def test_rm_refuses_dangerous_targets(self, h, root):
        h.run("cd sub")
        out = h.run("rm -rf ..")
        assert "Refusing" in out and (root / "sub").exists()

    def test_help_lists_every_command(self, h):
        out = h.run("help")
        for name in build_registry().names():
            assert name in out


class TestErrorsNeverKillTheShell:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("cd nope", "not found"),
            ("cd 'a b.txt'", "not a directory"),
            ("cat sub", "is a directory"),
            ("cat", "required"),
            ("rm", "required"),
            ("ls --bogus", "unrecognized"),
            ('echo "unterminated', "syntax error"),
            ("definitely_not_a_command_xyz", "command not found"),
            ("ai", "required"),
            ("ai agent -p 9", "invalid choice"),
            ("launch --file nope_missing.exe", "launch"),
            ("help nope", "unknown command"),
            ("stat /", "virtual root"),
        ],
    )
    def test_error_is_reported(self, h, line, expected):
        assert expected in h.run(line)

    def test_unexpected_exception_is_caught(self, h):
        def boom(ctx, args):
            raise RuntimeError("kaboom")

        h.shell.registry.get("pwd").handler = boom
        assert "RuntimeError: kaboom" in h.run("pwd")

    def test_keyboard_interrupt_in_a_command_is_caught(self, h):
        def interrupted(ctx, args):
            raise KeyboardInterrupt

        h.shell.registry.get("pwd").handler = interrupted
        assert "interrupted" in h.run("pwd")

    def test_blank_line_is_ignored(self, h):
        assert h.run("   ") == ""

    def test_help_flag_prints_usage_and_does_not_exit(self, h, capsys):
        h.run("ls -h")
        assert "usage: ls" in capsys.readouterr().out
        assert h.shell.context.running


def test_exit_stops_the_loop(h):
    h.run("exit")
    assert h.shell.context.running is False


def test_windows_backslash_paths_work(h, root):
    h.run(f"cd {root}\\sub".replace("/", "\\"))
    assert h.shell.files.location.endswith("/sub")


class TestRunLoop:
    """Shell.run() with a scripted prompt (no real terminal needed)."""

    @pytest.fixture
    def script(self, h, monkeypatch):
        from custom_console.shell import repl

        def run(*inputs):
            queue = iter(inputs)

            def fake_prompt(*args, **kwargs):
                item = next(queue)
                if isinstance(item, type) and issubclass(item, BaseException):
                    raise item
                return item

            monkeypatch.setattr(repl, "prompt", fake_prompt)
            h.buffer.seek(0)
            h.buffer.truncate()
            h.shell.run()
            return h.buffer.getvalue()

        return run

    def test_exit_command_ends_the_loop(self, script):
        assert "hello" in script("echo hello", "exit", "echo never reached")

    def test_end_of_input_ends_the_loop(self, script):
        script("pwd", EOFError)  # must return instead of raising

    def test_ctrl_c_at_the_prompt_keeps_the_loop_alive(self, script):
        out = script(KeyboardInterrupt, "echo still here", "exit")
        assert "still here" in out

    def test_location_is_printed_before_every_prompt(self, script, root):
        out = script("echo a", "echo b", "exit")
        assert out.count(str(root).replace("\\", "/")) == 3

    def test_location_shows_permissions_on_local_paths(self, script):
        assert " rwx" in script("exit").splitlines()[0]

    def test_location_inside_the_virtual_root_has_no_permissions(self, script):
        out = script("cd /", "exit")
        assert out.splitlines()[-1] == "/"

    def test_a_blank_line_separates_commands_except_after_clear(self, script):
        out = script("echo a", "clear", "echo b", "exit")
        assert "a\n\n" in out  # blank line after a command
        assert out.index("b") > out.index("a")

    def test_errors_do_not_stop_the_loop(self, script):
        out = script("cd nope", "echo survived", "exit")
        assert "not found" in out and "survived" in out


class TestFormatError:
    def test_oserror_uses_filename_not_errno_text(self):
        error = FileNotFoundError(2, "The system cannot find the file specified", "x.txt")
        assert format_error("cat", error) == "cat: x.txt: not found"

    def test_unknown_exception_shows_its_type(self):
        assert format_error("cmd", KeyError("k")) == "cmd: KeyError: 'k'"
