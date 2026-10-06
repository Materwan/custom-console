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
        """Hand a self-contained job to a sub-agent, which works with a fresh context and
        returns only its final report. Use it for searches or reading that would fill your
        context (exploring a folder, finding where something is done, summarising long
        files), not for a single quick lookup. The sub-agent does not see this conversation.

        Args:
            description: a few words naming the job (shown to the user).
            prompt: everything the sub-agent needs: the goal, paths, what to report back.
            allow_writes: let it change files and run commands too (default: read-only tools).
        """
        if not prompt.strip():
            raise ValueError("The prompt is empty: say what the sub-agent must do.")
        return ctx.run_subagent(description.strip() or "task", prompt, allow_writes)

    return [task]
