"""Tools exposed to the agent, grouped by theme."""

from typing import Callable, List, Tuple

from ..results import ToolResult
from .ask import ask_tools
from .base import ToolContext, guarded
from .filesystem import filesystem_tools
from .mail import mail_tools
from .moodle import moodle_tools
from .pdf import pdf_tools
from .shell import shell_tools
from .task import task_tools
from .todo import todo_tools
from .web import web_tools


# Label shown by /tools, and the factory of each group of tools.
TOOL_GROUPS = (
    ("Files", filesystem_tools),
    ("Commands", shell_tools),
    ("Checklist", todo_tools),
    ("Questions", ask_tools),
    ("Sub-agents", task_tools),
    ("Documents", pdf_tools),
    ("Web", web_tools),
    ("Mail", mail_tools),
    ("Moodle", moodle_tools),
)


def build_tool_groups(ctx: ToolContext) -> List[Tuple[str, List[Callable[..., ToolResult]]]]:
    """The tools enabled by the current settings, by group (empty groups left out)."""
    groups = [(label, list(factory(ctx))) for label, factory in TOOL_GROUPS]
    return [(label, tools) for label, tools in groups if tools]


def build_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    """Every tool enabled by the current settings."""
    return [tool for _, tools in build_tool_groups(ctx) for tool in tools]


__all__ = ["ToolContext", "ToolResult", "build_tool_groups", "build_tools", "guarded"]
