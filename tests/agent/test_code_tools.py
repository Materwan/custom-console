from __future__ import annotations

import sys
import threading
import time

import pytest

from custom_console.agent.permissions import UserPermissionDenied
from custom_console.agent.tools.shell import (
    HEAD_CHARS,
    MAX_OUTPUT_CHARS,
    run_shell,
    shell_tools,
    shorten_output,
)
from custom_console.agent.tools.state import ReadTracker, TodoList
from custom_console.agent.tools.todo import todo_tools

PYTHON = f'"{sys.executable}"'


def by_name(tools):
    return {tool.__name__: tool for tool in tools}


# --------------------------------------------------------------------------- #
# run_command
# --------------------------------------------------------------------------- #


class TestRunShell:
    def test_output_and_exit_code(self, tmp_path):
        code, output, reason = run_shell(f'{PYTHON} -c "print(123)"', str(tmp_path), 20)
        assert (code, output.strip(), reason) == (0, "123", "")

    def test_stderr_is_merged_and_exit_code_kept(self, tmp_path):
        command = f"{PYTHON} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""
        code, output, _ = run_shell(command, str(tmp_path), 20)
        assert code == 3 and "out" in output and "err" in output

    def test_runs_in_the_given_directory(self, tmp_path):
        code, output, _ = run_shell(f'{PYTHON} -c "import os; print(os.getcwd())"', str(tmp_path), 20)
        assert output.strip().lower() == str(tmp_path).lower()

    def test_no_keyboard_input_is_available(self, tmp_path):
        code, output, _ = run_shell(f"{PYTHON} -c \"import sys; print(repr(sys.stdin.read()))\"", str(tmp_path), 20)
        assert code == 0 and "''" in output  # immediate end of input, no hang

    def test_timeout_kills_the_command(self, tmp_path):
        started = time.monotonic()
        code, _, reason = run_shell(f'{PYTHON} -c "import time; time.sleep(30)"', str(tmp_path), 1)
        assert (code, reason) == (None, "timeout") and time.monotonic() - started < 10

    def test_cancellation_kills_the_command(self, tmp_path):
        cancelled = threading.Event()
        threading.Timer(0.5, cancelled.set).start()
        started = time.monotonic()
        code, _, reason = run_shell(f'{PYTHON} -c "import time; time.sleep(30)"', str(tmp_path), 60, cancelled.is_set)
        assert (code, reason) == (None, "cancelled") and time.monotonic() - started < 10

    def test_long_output_keeps_head_and_tail(self):
        text = "H" * 10_000 + "M" * 50_000 + "TAIL"
        short = shorten_output(text)
        assert len(short) < MAX_OUTPUT_CHARS + 100
        assert short.startswith("H" * HEAD_CHARS) and short.endswith("TAIL") and "characters skipped" in short
        assert shorten_output("small") == "small"


class TestRunCommandTool:
    def test_success(self, make_ctx, files_dir):
        ctx, log = make_ctx(auto_level=2)
        result = by_name(shell_tools(ctx))["run_command"](f'{PYTHON} -c "print(6*7)"')
        assert result.success and result.data == {"exit_code": 0, "output": "42"}

    def test_a_failing_command_is_a_failed_result_that_still_carries_the_output(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)
        result = by_name(shell_tools(ctx))["run_command"](f"{PYTHON} -c \"print('boom'); raise SystemExit(2)\"")
        assert not result.success and "exit code 2" in str(result.error)
        assert result.data == {"exit_code": 2, "output": "boom"}

    def test_timeout_is_reported(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)
        result = by_name(shell_tools(ctx))["run_command"](f'{PYTHON} -c "import time; time.sleep(30)"', timeout=1)
        assert isinstance(result.error, TimeoutError) and "1s" in str(result.error)

    def test_it_asks_even_inside_the_free_zone(self, make_ctx):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        result = by_name(shell_tools(ctx))["run_command"]("echo hi")
        assert isinstance(result.error, UserPermissionDenied)
        assert "run a command" in log.asked[0] and "echo hi" in log.asked[0]

    def test_it_runs_without_asking_at_level_2(self, make_ctx):
        ctx, log = make_ctx(zone=True, auto_level=2, answer=False)
        assert by_name(shell_tools(ctx))["run_command"]("echo hi").success and log.asked == []

    def test_it_runs_in_the_agents_working_directory(self, make_ctx, files_dir):
        ctx, _ = make_ctx(auto_level=2)
        (files_dir / "sub").mkdir()
        ctx.files.change_directory("sub")
        result = by_name(shell_tools(ctx))["run_command"](f'{PYTHON} -c "import os; print(os.getcwd())"')
        assert result.data["output"].replace("\\", "/").lower().endswith("/files/sub")

    def test_it_refuses_to_run_on_the_remarkable_or_the_virtual_root(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)
        ctx.files.mode = ctx.files.MODE_ROOT
        result = by_name(shell_tools(ctx))["run_command"]("echo hi")
        assert isinstance(result.error, ValueError) and "local folder" in str(result.error)

    def test_the_timeout_is_clamped(self, make_ctx, monkeypatch):
        from custom_console.agent.tools import shell as shell_module

        seen = {}
        monkeypatch.setattr(shell_module, "run_shell", lambda command, cwd, timeout, cancelled: seen.update(timeout=timeout) or (0, "", ""))
        ctx, _ = make_ctx(auto_level=2)
        tool = by_name(shell_tools(ctx))["run_command"]
        tool("x", timeout=99999)
        assert seen["timeout"] == shell_module.MAX_TIMEOUT
        tool("x", timeout=-5)
        assert seen["timeout"] == 1


# --------------------------------------------------------------------------- #
# todo_write
# --------------------------------------------------------------------------- #


class TestTodoList:
    def test_replace_and_render(self):
        todos = TodoList()
        todos.replace(
            [
                {"content": "read files", "status": "completed"},
                {"content": "edit code", "status": "in_progress"},
                {"content": "run tests"},
            ]
        )
        assert todos.render().splitlines() == ["☑ read files", "◐ edit code", "☐ run tests"]
        assert todos.summary() == "1/3 done"

    def test_plain_strings_and_alternative_keys_are_accepted(self):
        todos = TodoList()
        todos.replace(["first", {"task": "second", "status": "in-progress"}, {"title": "third", "status": "Pending"}])
        assert [(t.content, t.status) for t in todos.items] == [
            ("first", "pending"),
            ("second", "in_progress"),
            ("third", "pending"),
        ]

    @pytest.mark.parametrize(
        "bad, message",
        [
            ([{"content": "x", "status": "done"}], "Invalid status"),
            ([{"status": "pending"}], "non-empty"),
            ([{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}], "Only one"),
            ([42], "must be an object"),
        ],
    )
    def test_invalid_lists_are_refused_and_keep_the_previous_one(self, bad, message):
        todos = TodoList()
        todos.replace(["keep me"])
        with pytest.raises(ValueError, match=message):
            todos.replace(bad)
        assert [t.content for t in todos.items] == ["keep me"]


class TestTodoTool:
    def test_the_tool_returns_the_checklist_for_display_and_a_short_answer_for_the_model(self, ctx):
        result = by_name(todo_tools(ctx))["todo_write"]([{"content": "a", "status": "completed"}, {"content": "b"}])
        assert result.data == "1/2 done" and result.todos == "☑ a\n☐ b"
        assert result.to_llm() == '{"success":true,"data":"1/2 done"}'

    def test_an_empty_list_clears_the_checklist(self, ctx):
        tool = by_name(todo_tools(ctx))["todo_write"]
        tool(["a"])
        assert tool([]).todos == "" and ctx.todos.items == []

    def test_it_never_asks(self, make_ctx):
        ctx, log = make_ctx(auto_level=0, answer=False)
        assert by_name(todo_tools(ctx))["todo_write"](["a"]).success and log.asked == []

    def test_invalid_input_is_a_failed_result(self, ctx):
        assert not by_name(todo_tools(ctx))["todo_write"]([{"content": "a", "status": "nope"}]).success


# --------------------------------------------------------------------------- #
# ReadTracker
# --------------------------------------------------------------------------- #


class TestReadTracker:
    def test_unread_changed_and_fresh_files(self, tmp_path):
        import os

        target = tmp_path / "f.txt"
        target.write_text("x")
        tracker = ReadTracker()
        with pytest.raises(PermissionError, match="not been read"):
            tracker.check(str(target))
        tracker.mark(str(target))
        tracker.check(str(target))
        target.write_text("y")
        os.utime(target, (2_000_000_000, 2_000_000_000))
        with pytest.raises(PermissionError, match="changed since"):
            tracker.check(str(target))

    def test_paths_are_compared_normalised(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.mark(str(target))
        tracker.check(str(tmp_path / "sub" / ".." / "f.txt"))

    def test_clear_forgets_everything(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x")
        tracker = ReadTracker()
        tracker.mark(str(target))
        tracker.clear()
        with pytest.raises(PermissionError):
            tracker.check(str(target))
