from __future__ import annotations

from typing import List, Optional

import pytest

from custom_console.agent.permissions import (
    PROJECT,
    SESSION,
    ApprovalRules,
    Decision,
    PermissionGate,
    PermissionLevel,
    UserPermissionDenied,
    command_rule,
    describe_call,
    is_yes,
    parse_decision,
    rule_label,
)
from custom_console.agent.results import ToolResult
from custom_console.agent.tools import guarded


class TestToolResult:
    def test_ok_omits_unset_fields(self):
        assert ToolResult.ok().to_dict() == {"success": True}
        assert ToolResult.ok([1, 2]).to_dict() == {"success": True, "data": [1, 2]}

    def test_falsy_data_is_kept(self):
        assert ToolResult.ok(0).to_dict() == {"success": True, "data": 0}
        assert ToolResult.ok("").to_dict() == {"success": True, "data": ""}

    def test_fail_names_the_error_type(self):
        result = ToolResult.fail(FileNotFoundError("x.txt"))
        assert result.to_dict() == {"success": False, "error": "FileNotFoundError: x.txt"}

    def test_partial_data_on_failure(self):
        assert ToolResult.fail(ValueError("v"), data=["partial"]).to_dict()["data"] == ["partial"]

    def test_to_llm_is_plain_text(self):
        assert ToolResult.ok('def f():\n    return "x"').to_llm() == 'def f():\n    return "x"'  # nothing escaped
        assert ToolResult.ok(["a", "b"]).to_llm() == "a\nb"
        assert ToolResult.ok().to_llm() == "Done." and ToolResult.ok("").to_llm() == "(empty)"

    def test_a_mapping_gives_key_lines_then_its_long_texts(self):
        text = ToolResult.ok({"exit_code": 0, "output": "line 1\nline 2", "name": "é", "ok": True}).to_llm()
        assert text == "exit_code: 0\nname: é\nok: true\noutput:\nline 1\nline 2"

    def test_a_failure_starts_with_error_and_keeps_partial_data(self):
        text = ToolResult.fail(RuntimeError("exit code 1"), {"output": "boom\ntrace"}).to_llm()
        assert text == "Error: RuntimeError: exit code 1\noutput:\nboom\ntrace"

    def test_to_llm_survives_non_json_values(self):
        from pathlib import Path

        assert "tmp" in ToolResult.ok(Path("tmp")).to_llm()


class TestPermissionHelpers:
    @pytest.mark.parametrize("answer", ["", " ", "y", "Y", "yes", "o", "OUI"])
    def test_yes(self, answer):
        assert is_yes(answer)

    @pytest.mark.parametrize("answer", ["n", "no", "non", "maybe", "nope"])
    def test_no(self, answer):
        assert not is_yes(answer)

    def test_describe_call(self):
        assert describe_call("file_system_pwd", {}) == "Agent wants to file system pwd."
        assert describe_call("file_system_list", {"path": ".", "show_hidden": True}) == (
            "Agent wants to file system list with path='.', show_hidden=True."
        )

    def test_describe_call_shortens_long_values(self):
        info = describe_call("workspace_file_write", {"content": "x" * 5000}, value_limit=50)
        assert len(info) < 150 and "…" in info


class TestPermissionGate:
    def make(self, auto_level, answer=True):
        asked, recorded = [], []
        gate = PermissionGate(
            auto_level,
            ask=lambda info, rule=None: asked.append(info) or answer,
            record=lambda info, status: recorded.append((info, status)),
        )
        return gate, asked, recorded

    def test_auto_accepts_up_to_the_level_and_records_it(self):
        gate, asked, recorded = self.make(PermissionLevel.READ)
        assert bool(gate.request("read it", PermissionLevel.READ)) is True
        assert asked == [] and recorded == [("read it", "auto-accepted")]

    def test_asks_above_the_level(self):
        gate, asked, recorded = self.make(PermissionLevel.READ, answer=False)
        assert bool(gate.request("write it", PermissionLevel.WRITE)) is False
        assert asked == ["write it"] and recorded == [("write it", "refused")]

    def test_user_acceptance_is_recorded(self):
        gate, _, recorded = self.make(PermissionLevel.NONE, answer=True)
        assert bool(gate.request("read it", PermissionLevel.READ)) is True
        assert recorded == [("read it", "accepted")]

    def test_trivial_level_is_silent(self):
        gate, asked, recorded = self.make(PermissionLevel.NONE)
        assert bool(gate.request("pwd", PermissionLevel.NONE)) is True
        assert asked == [] and recorded == []

    def test_level_two_accepts_everything(self):
        gate, asked, _ = self.make(PermissionLevel.WRITE)
        assert gate.request("delete", PermissionLevel.WRITE)
        assert asked == []


class TestDecisions:
    def test_answers(self):
        assert parse_decision("y") == Decision(True) and parse_decision("oui") == Decision(True)
        assert parse_decision("a") == Decision(True, SESSION) and parse_decision("p") == Decision(True, PROJECT)
        assert parse_decision("n") == Decision(False) and parse_decision("") is None
        assert parse_decision("n, use pytest -x") == Decision(False, reason="use pytest -x")
        assert parse_decision("do it in src/ instead") == Decision(False, reason="do it in src/ instead")
        assert parse_decision("a", can_remember=False) == Decision(False, reason="a")

    def test_command_rules_name_the_program_and_its_subcommand(self):
        assert command_rule("git status -s") == "run_command:git status"
        assert command_rule("C:/Python/python.exe -m pytest -q") == "run_command:python -m pytest"
        assert command_rule("pytest tests/a.py") == "run_command:pytest"
        assert command_rule("git --no-pager log") == "run_command:git"

    def test_chained_or_redirected_commands_have_no_rule(self):
        for command in ("git status && del x", "dir | findstr a", "echo a > b", "a; b", "echo %PATH%", "x $(y)"):
            assert command_rule(command) is None, command

    def test_labels(self):
        assert rule_label("run_command:git status") == "`git status …` commands"
        assert rule_label("file_system_write:C:/notes") == "file_system_write in C:/notes"


class TestApprovalRules:
    def make(self, tmp_path, answer):
        asked = []
        rules = ApprovalRules(tmp_path / "rules.json")
        gate = PermissionGate(0, ask=lambda info, rule=None: asked.append(rule) or answer, rules=rules)
        return gate, asked

    def test_always_for_the_session_stops_the_questions_for_that_rule_only(self, tmp_path):
        gate, asked = self.make(tmp_path, Decision(True, SESSION))
        assert gate.request("a", PermissionLevel.WRITE, "run_command:git status")
        assert gate.request("b", PermissionLevel.WRITE, "run_command:git status")
        assert gate.request("c", PermissionLevel.WRITE, "run_command:git push")
        assert asked == ["run_command:git status", "run_command:git push"]
        assert ApprovalRules(tmp_path / "rules.json").project == set()  # not saved

    def test_always_for_the_project_is_saved(self, tmp_path):
        gate, _ = self.make(tmp_path, Decision(True, PROJECT))
        gate.request("a", PermissionLevel.WRITE, "send_email")
        again = ApprovalRules(tmp_path / "rules.json")
        assert again.allows("send_email") == PROJECT and again.listed() == ["send_email (project)"]
        again.forget()
        assert ApprovalRules(tmp_path / "rules.json").allows("send_email") == ""

    def test_a_call_without_a_rule_is_always_asked(self, tmp_path):
        gate, asked = self.make(tmp_path, Decision(True, SESSION))
        gate.request("a", PermissionLevel.WRITE, None)
        gate.request("a", PermissionLevel.WRITE, None)
        assert asked == [None, None]

    def test_a_refusal_with_a_reason_is_recorded_and_told_to_the_model(self, make_ctx):
        ctx, log = make_ctx(auto_level=0, answer=Decision(False, reason="not now"))

        @guarded(ctx, PermissionLevel.WRITE)
        def send(text: str) -> ToolResult:
            return ToolResult.ok("sent")

        result = send("hi")
        assert not result.success and str(result.error) == "The user refused: send. They said: not now"
        assert log.recorded[-1][1] == "refused: not now" and log.rules == ["send"]


class TestArgumentCoercion:
    def test_values_a_model_sends_as_text_are_converted(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)
        seen = {}

        @guarded(ctx, PermissionLevel.NONE)
        def tool(depth: int = 1, hidden: bool = False, ratio: float = 0.0, pages: Optional[List[int]] = None) -> ToolResult:
            seen.update(depth=depth, hidden=hidden, ratio=ratio, pages=pages)
            return ToolResult.ok()

        assert tool(depth="3", hidden="true", ratio="0.5", pages="[1, 2]").success
        assert seen == {"depth": 3, "hidden": True, "ratio": 0.5, "pages": [1, 2]}
        tool(pages=4, depth=2.0)
        assert seen["pages"] == [4] and seen["depth"] == 2

    def test_what_does_not_convert_is_left_for_the_tool_to_refuse(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)

        @guarded(ctx, PermissionLevel.NONE)
        def tool(depth: int = 1) -> ToolResult:
            return ToolResult.ok(depth + 1)

        assert not tool(depth="deep").success


class TestGuardedDecorator:
    def test_denied_call_does_not_run_and_reports_the_refusal(self, make_ctx):
        ctx, log = make_ctx(auto_level=0, answer=False)
        ran = []

        @guarded(ctx, PermissionLevel.WRITE)
        def dangerous(path: str) -> ToolResult:
            ran.append(path)
            return ToolResult.ok()

        result = dangerous(path="/x")
        assert not result.success and isinstance(result.error, UserPermissionDenied)
        assert ran == [] and log.asked == ["Agent wants to dangerous with path='/x'."]

    def test_allowed_call_runs(self, make_ctx):
        ctx, _ = make_ctx(auto_level=2)

        @guarded(ctx, PermissionLevel.WRITE)
        def fine(value: int = 3) -> ToolResult:
            return ToolResult.ok(value * 2)

        assert fine(4).data == 8 and fine().data == 6

    def test_exceptions_become_failed_results(self, make_ctx):
        ctx, _ = make_ctx()

        @guarded(ctx, PermissionLevel.READ)
        def broken() -> ToolResult:
            raise FileNotFoundError("gone")

        result = broken()
        assert not result.success and isinstance(result.error, FileNotFoundError)

    def test_custom_description(self, make_ctx):
        ctx, log = make_ctx(auto_level=0, answer=True)

        @guarded(ctx, PermissionLevel.READ, describe=lambda path: f"open {path}")
        def tool(path: str) -> ToolResult:
            return ToolResult.ok()

        tool("a")
        assert log.asked == ["open a"]

    def test_signature_and_docstring_survive_for_the_schema(self, make_ctx):
        import inspect

        ctx, _ = make_ctx()

        @guarded(ctx, PermissionLevel.READ)
        def tool(path: str, depth: int = 2) -> ToolResult:
            """Doc."""

        assert list(inspect.signature(tool).parameters) == ["path", "depth"]
        assert tool.__doc__ == "Doc." and tool.__name__ == "tool"
