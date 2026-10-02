"""The description of each tool that the model reads, built from the function itself."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional, Union

import pytest

from custom_console.agent.schema import tool_schema, tools_token_estimate
from custom_console.agent.tools import build_tools


def sample(
    path: str,
    count: int = 3,
    flag: bool = False,
    ratio: float = 1.0,
    names: Optional[List[str]] = None,
    mode: Literal["full", "range"] = "full",
    data: Optional[Dict[str, Any]] = None,
    mixed: Optional[List[Union[str, Dict[str, Any]]]] = None,
):
    """Do a thing. It spans
    two lines.

    A second paragraph.

    Args:
        path: the file to use.
        count: how many; the text goes on
            over a second line.
        flag: a switch.
        names: some names.
        mode: how to read.
    """


class TestTypes:
    def schema(self):
        return tool_schema(sample)["function"]["parameters"]

    def test_the_name_and_the_description(self):
        function = tool_schema(sample)["function"]
        assert tool_schema(sample)["type"] == "function" and function["name"] == "sample"
        assert function["description"] == "Do a thing. It spans two lines.\n\nA second paragraph."

    def test_only_parameters_without_a_default_are_required(self):
        assert self.schema()["required"] == ["path"]

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("path", {"type": "string"}),
            ("count", {"type": "integer"}),
            ("flag", {"type": "boolean"}),
            ("ratio", {"type": "number"}),
            ("names", {"type": "array", "items": {"type": "string"}}),
            ("mode", {"enum": ["full", "range"]}),
            ("data", {"type": "object"}),
            ("mixed", {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}}),
        ],
    )
    def test_python_types_become_json_types(self, name, expected):
        property_ = dict(self.schema()["properties"][name])
        property_.pop("description", None)
        assert property_ == expected

    def test_parameter_descriptions_come_from_the_args_block(self):
        properties = self.schema()["properties"]
        assert properties["path"]["description"] == "the file to use."
        assert properties["count"]["description"] == "how many; the text goes on over a second line."
        assert "description" not in properties["ratio"]  # not documented

    def test_a_tool_without_documentation_or_hints_still_has_a_schema(self):
        def bare(a, b=1):
            pass

        function = tool_schema(bare)["function"]
        assert function["description"] == "" and function["parameters"]["required"] == ["a"]
        assert function["parameters"]["properties"] == {"a": {}, "b": {}}


class TestRealTools:
    def tools(self, make_ctx):
        ctx, _ = make_ctx(MOODLE_ENABLED="false")
        return build_tools(ctx)

    def test_a_guarded_tool_is_described_through_its_wrapper(self, make_ctx):
        edit = next(tool for tool in self.tools(make_ctx) if tool.__name__ == "file_system_edit")
        function = tool_schema(edit)["function"]
        assert function["name"] == "file_system_edit"
        assert function["description"].startswith("Replace exact text in a file.")
        assert function["parameters"]["required"] == ["path", "old_text", "new_text"]
        assert function["parameters"]["properties"]["replace_all"]["type"] == "boolean"
        assert function["parameters"]["properties"]["old_text"]["description"].startswith("the exact text to replace")

    def test_a_multi_line_argument_description_is_kept_whole(self, make_ctx):
        todo = next(tool for tool in self.tools(make_ctx) if tool.__name__ == "todo_write")
        description = tool_schema(todo)["function"]["parameters"]["properties"]["todos"]["description"]
        assert description.startswith("the items") and description.endswith("clears the checklist.")
        assert tool_schema(todo)["function"]["parameters"]["properties"]["todos"]["type"] == "array"

    def test_every_tool_has_a_complete_valid_schema(self, make_ctx):
        tools = self.tools(make_ctx)
        names = [tool.__name__ for tool in tools]
        assert len(names) == len(set(names)) and len(names) > 15
        for tool in tools:
            function = tool_schema(tool)["function"]
            json.dumps(function)  # must be sendable
            assert function["name"] == tool.__name__ and len(function["description"]) > 20, tool.__name__
            properties = function["parameters"]["properties"]
            assert set(function["parameters"]["required"]) <= set(properties), tool.__name__
            for name, schema in properties.items():
                assert "description" in schema, f"{tool.__name__}({name}) is not documented in its Args block"

    def test_the_token_estimate_grows_with_the_tools(self, make_ctx):
        tools = self.tools(make_ctx)
        assert 0 < tools_token_estimate(tools[:3]) < tools_token_estimate(tools)
        assert tools_token_estimate([]) == 0
