"""`workspace_file_*` tools: the agent's own sandboxed folders ("result" and "tmp")."""

from typing import Callable, List

from ...fs import virtual
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, cap_items, guarded, truncation_notice

MAX_WORKSPACE_READ_CHARS = 12_000
MAX_ENTRIES = 150


def workspace_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    workspace = ctx.workspace

    # `directory` is the workspace: "result" (deliverables) or "tmp" (scratch).

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def workspace_file_list(directory: str, path: str = ".") -> ToolResult:
        """List a workspace folder. directory: "result" or "tmp"."""
        return ToolResult.ok(cap_items(workspace.list(directory, path), MAX_ENTRIES))

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def workspace_file_read(directory: str, path: str, encoding: str = "utf-8") -> ToolResult:
        """Read a workspace text file (long files are cut)."""
        text, truncated = workspace.read(directory, path, encoding, MAX_WORKSPACE_READ_CHARS)
        return ToolResult.ok(text + truncation_notice(MAX_WORKSPACE_READ_CHARS) if truncated else text)

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_write(
        directory: str, path: str, content: str, encoding: str = "utf-8", overwrite: bool = True
    ) -> ToolResult:
        """Write a workspace text file (creates folders; overwrite=False fails if it exists)."""
        return ToolResult.ok(workspace.describe(workspace.write(directory, path, content, encoding, overwrite)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_delete(directory: str, path: str) -> ToolResult:
        """Delete a workspace file or folder."""
        return ToolResult.ok(workspace.describe(workspace.delete(directory, path)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_move(directory: str, path: str, dst_directory: str, dst_path: str) -> ToolResult:
        """Move or rename inside or between workspaces."""
        return ToolResult.ok(workspace.describe(workspace.move(directory, path, dst_directory, dst_path)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_get(src: str, dst_directory: str) -> ToolResult:
        """Copy a file or folder from the system file system (disks, WSL, reMarkable) into a
        workspace; use "result" to hand a file to the user."""
        destination = workspace.resolve(dst_directory, ".")
        # Expressed as an absolute virtual path so that it stays local even if
        # the agent navigated into reMarkable.
        count = ctx.files.copy(src, virtual.local_to_virtual(str(destination)), recursive=True)
        return ToolResult.ok(f"copied {count} file(s) to {workspace.describe(destination)}")

    return [
        workspace_file_list,
        workspace_file_read,
        workspace_file_write,
        workspace_file_delete,
        workspace_file_move,
        workspace_file_get,
    ]
