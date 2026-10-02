"""ToolSet: which tools the agent may use, and how the choice is kept."""

from __future__ import annotations

import json

from custom_console.agent.toolset import ToolSet
from custom_console.agent.tools import build_tool_groups


def tool(name, doc="Does the thing. More detail follows."):
    def function():
        pass

    function.__name__ = name
    function.__doc__ = doc
    return function


def sample(path=None):
    return ToolSet(
        [
            ("Files", [tool("file_read"), tool("file_write")]),
            ("Commands", [tool("run_command")]),
            ("Empty", []),
        ],
        path,
    )


class TestSelection:
    def test_everything_is_on_by_default_and_empty_groups_are_dropped(self):
        tools = sample()
        assert [label for label, _ in tools.groups] == ["Files", "Commands"]
        assert tools.names() == ["file_read", "file_write", "run_command"]
        assert [t.__name__ for t in tools.enabled()] == tools.names() and tools.disabled_names() == []

    def test_turning_tools_off_and_on(self):
        tools = sample()
        tools.set_enabled({"file_write": False, "run_command": False})
        assert [t.__name__ for t in tools.enabled()] == ["file_read"]
        assert tools.disabled_names() == ["file_write", "run_command"]
        tools.set_enabled({"file_write": True})
        assert tools.is_enabled("file_write") and not tools.is_enabled("run_command")
        tools.reset()
        assert tools.disabled_names() == []

    def test_unknown_names_are_ignored(self):
        tools = sample()
        tools.set_enabled({"nothing": False})
        assert tools.disabled_names() == []

    def test_group_and_description(self):
        tools = sample()
        assert tools.group_of("run_command") == "Commands"
        assert ToolSet.describe(tools.tools()[0]) == "Does the thing"


class TestResolve:
    def test_names_groups_and_unique_prefixes(self):
        tools = sample()
        assert tools.resolve(["run_command"]) == (["run_command"], [])
        assert tools.resolve(["FILES"]) == (["file_read", "file_write"], [])  # a group, any case
        assert tools.resolve(["run_"]) == (["run_command"], [])
        assert tools.resolve(["file_r"]) == (["file_read"], [])

    def test_ambiguous_and_unknown_words_are_reported(self):
        tools = sample()
        assert tools.resolve(["file_", "bogus"]) == ([], ["file_", "bogus"])  # `file_` matches two tools
        assert tools.resolve(["files", "file_read", "x"]) == (["file_read", "file_write"], ["x"])  # no duplicates


class TestPersistence:
    def test_the_choice_survives_a_restart(self, tmp_path):
        path = tmp_path / "agent" / "tools.json"
        sample(path).set_enabled({"run_command": False})
        assert json.loads(path.read_text(encoding="utf-8")) == {"disabled": ["run_command"]}
        assert sample(path).disabled_names() == ["run_command"]

    def test_a_tool_that_is_absent_for_a_while_stays_off(self, tmp_path):
        path = tmp_path / "tools.json"
        path.write_text(json.dumps({"disabled": ["moodle_list_courses", "run_command"]}), encoding="utf-8")
        tools = sample(path)
        assert tools.disabled_names() == ["run_command"]
        tools.set_enabled({"file_write": False})
        assert set(json.loads(path.read_text(encoding="utf-8"))["disabled"]) == {"moodle_list_courses", "run_command", "file_write"}

    def test_a_damaged_file_means_everything_is_on(self, tmp_path):
        for content in ("{not json", "[]", '{"disabled": 3}'):
            path = tmp_path / "tools.json"
            path.write_text(content, encoding="utf-8")
            assert sample(path).disabled_names() == []

    def test_an_unwritable_location_does_not_break_the_choice(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        tools = sample(blocker / "tools.json")  # a folder cannot be made under a file
        tools.set_enabled({"run_command": False})
        assert tools.disabled_names() == ["run_command"]


class TestRealTools:
    def test_groups_of_the_real_tools(self, ctx):
        groups = dict(build_tool_groups(ctx))
        assert {"Files", "Commands", "Checklist", "Documents", "Web"} <= set(groups)
        assert "file_system_read" in [t.__name__ for t in groups["Files"]]
        assert {"pdf_to_markdown", "rmdoc_to_pdf"} <= {t.__name__ for t in groups["Documents"]}

    def test_every_real_tool_has_a_one_line_description(self, ctx):
        for label, tools in build_tool_groups(ctx):
            for real in tools:
                assert ToolSet.describe(real) and "\n" not in ToolSet.describe(real), real.__name__
