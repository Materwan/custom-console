"""`task`: hand a self-contained job to a sub-agent (see `agent.subagent`)."""

from typing import Callable, List

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded


def task_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    if ctx.run_subagent is None:
        return []

    @guarded(ctx, PermissionLevel.NONE)  # each tool of the sub-agent asks for itself
    def task(description: str, prompt: str, allow_writes: bool = False) -> ToolResult:
        """Hand a self-contained job to a sub-agent with a fresh context; only its final report comes back. For searches or reading that would fill your context, not a quick lookup. It does not see this conversation.

        Args:
            description: a few words naming the job
            prompt: everything it needs: goal, paths, what to report
            allow_writes: let it change files and run commands (default read-only)
        """
        if not prompt.strip():
            raise ValueError("The prompt is empty: say what the sub-agent must do.")
        return ctx.run_subagent(description.strip() or "task", prompt, allow_writes)

    return [task]
