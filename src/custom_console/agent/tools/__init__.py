"""Tools exposed to the agent, grouped by theme."""

from typing import Callable, List

from ..results import ToolResult
from .base import ToolContext, guarded
from .filesystem import filesystem_tools
from .mail import mail_tools
from .moodle import moodle_tools
from .pdf import pdf_tools
from .web import web_tools
from .workspace_tools import workspace_tools


def build_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    """Every tool enabled by the current settings."""
    tools: List[Callable[..., ToolResult]] = []
    for factory in (filesystem_tools, workspace_tools, pdf_tools, web_tools, mail_tools, moodle_tools):
        tools.extend(factory(ctx))
    return tools


__all__ = ["ToolContext", "ToolResult", "build_tools", "guarded"]
