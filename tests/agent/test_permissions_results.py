from __future__ import annotations

import json

import pytest

from custom_console.agent.permissions import (
    PermissionGate,
    PermissionLevel,
    UserPermissionDenied,
    describe_call,
    is_yes,
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

    def test_to_llm_is_compact_json_with_unicode(self):
        text = ToolResult.ok({"name": "é", "path": object}).to_llm()
        assert json.loads(text)["name"] == "é"  # no {"success", "data"} envelope
        assert "\\u" not in text and ": " not in text and ", " not in text

    def test_to_llm_is_terse(self):
        assert ToolResult.ok("plain text").to_llm() == "plain text"  # strings are not JSON-quoted
        assert ToolResult.ok().to_llm() == "ok" and ToolResult.ok("").to_llm() == "ok"
        assert ToolResult.ok([]).to_llm() == "[]"
        assert ToolResult.fail(FileNotFoundError("x.txt")).to_llm() == "ERROR FileNotFoundError: x.txt"
        assert ToolResult.fail(RuntimeError()).to_llm() == "ERROR RuntimeError"

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
            ask=lambda info: asked.append(info) or answer,
            record=lambda info, status: recorded.append((info, status)),
        )
        return gate, asked, recorded

    def test_auto_accepts_up_to_the_level_and_records_it(self):
        gate, asked, recorded = self.make(PermissionLevel.READ)
        assert gate.request("read it", PermissionLevel.READ) is True
        assert asked == [] and recorded == [("read it", "auto-accepted")]

    def test_asks_above_the_level(self):
        gate, asked, recorded = self.make(PermissionLevel.READ, answer=False)
        assert gate.request("write it", PermissionLevel.WRITE) is False
        assert asked == ["write it"] and recorded == [("write it", "refused")]

    def test_user_acceptance_is_recorded(self):
        gate, _, recorded = self.make(PermissionLevel.NONE, answer=True)
        assert gate.request("read it", PermissionLevel.READ) is True
        assert recorded == [("read it", "accepted")]

    def test_trivial_level_is_silent(self):
        gate, asked, recorded = self.make(PermissionLevel.NONE)
        assert gate.request("pwd", PermissionLevel.NONE) is True
        assert asked == [] and recorded == []

    def test_level_two_accepts_everything(self):
        gate, asked, _ = self.make(PermissionLevel.WRITE)
        assert gate.request("delete", PermissionLevel.WRITE)
        assert asked == []


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
