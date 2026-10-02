"""`todo_write`: the agent's checklist for multi-step tasks."""

from typing import Any, Callable, Dict, List

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded


def todo_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    @guarded(ctx, PermissionLevel.NONE)
    def todo_write(todos: List[Dict[str, Any]]) -> ToolResult:
        """Write or update your checklist for a task that takes several steps (3 or more).
        Each call replaces the whole list, so resend every item. Mark an item
        in_progress when you start it and completed as soon as it is done; keep at most
        one item in_progress.

        Args:
            todos: the items, each {"content": "what to do", "status": "pending" |
                "in_progress" | "completed"}. An empty list clears the checklist.
        """
        ctx.todos.replace(todos)
        checklist = ctx.todos.render()
        return ToolResult.ok(ctx.todos.summary(), todos=checklist, summary=ctx.todos.summary(), detail=checklist)

    return [todo_write]
