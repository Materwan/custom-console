"""`todo_write`: the agent's checklist for multi-step tasks."""

from typing import Any, Callable, Dict, List

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded


def todo_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    @guarded(ctx, PermissionLevel.NONE)
    def todo_write(todos: List[Dict[str, Any]]) -> ToolResult:
        """Checklist for a task of 3 or more steps; each call replaces the whole list. One item in_progress at most; mark completed as soon as done.

        Args:
            todos: the items, each {"content", "status": "pending" | "in_progress" | "completed"}. An empty list clears the checklist.
        """
        ctx.todos.replace(todos)
        checklist = ctx.todos.render()
        return ToolResult.ok(ctx.todos.summary(), todos=checklist, summary=ctx.todos.summary(), detail=checklist)

    return [todo_write]
