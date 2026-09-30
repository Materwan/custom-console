"""`workspace_file_*` tools: the agent's own sandboxed folders ("result" and "tmp")."""

from typing import Callable, List

from ...fs import virtual
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded, truncation_notice

MAX_WORKSPACE_READ_CHARS = 50_000


def workspace_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    workspace = ctx.workspace

    @guarded(ctx, PermissionLevel.READ)
    def workspace_file_list(directory: str, path: str = ".") -> ToolResult:
        """List a folder of a workspace.

        Args:
            directory: workspace to use: "result" (deliverables) or "tmp" (scratch).
            path: folder inside that workspace.
        """
        return ToolResult.ok(workspace.list(directory, path))

    @guarded(ctx, PermissionLevel.READ)
    def workspace_file_read(directory: str, path: str, encoding: str = "utf-8") -> ToolResult:
        """Read a text file of a workspace.

        Args:
            directory: "result" or "tmp".
            path: file inside that workspace.
            encoding: text encoding.
        """
        text, truncated = workspace.read(directory, path, encoding, MAX_WORKSPACE_READ_CHARS)
        return ToolResult.ok(text + truncation_notice(MAX_WORKSPACE_READ_CHARS) if truncated else text)

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_write(
        directory: str, path: str, content: str, encoding: str = "utf-8", overwrite: bool = True
    ) -> ToolResult:
        """Create or replace a text file in a workspace (missing folders are created).

        Args:
            directory: "result" (final deliverables) or "tmp" (scratch files).
            path: file inside that workspace.
            content: the text to write.
            encoding: text encoding.
            overwrite: when False, fail instead of replacing an existing file.
        """
        return ToolResult.ok(workspace.describe(workspace.write(directory, path, content, encoding, overwrite)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_delete(directory: str, path: str) -> ToolResult:
        """Delete a file or a folder of a workspace.

        Args:
            directory: "result" or "tmp".
            path: file or folder to delete (the workspace root itself is refused).
        """
        return ToolResult.ok(workspace.describe(workspace.delete(directory, path)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_move(directory: str, path: str, dst_directory: str, dst_path: str) -> ToolResult:
        """Move or rename a file or folder, possibly between workspaces.

        Args:
            directory: source workspace ("result" or "tmp").
            path: source path inside it.
            dst_directory: destination workspace.
            dst_path: destination path inside it.
        """
        return ToolResult.ok(workspace.describe(workspace.move(directory, path, dst_directory, dst_path)))

    @guarded(ctx, PermissionLevel.WRITE)
    def workspace_file_get(src: str, dst_directory: str) -> ToolResult:
        """Copy a file or folder from the system file system (disks, WSL, reMarkable)
        into a workspace. Use it to hand a file over to the user.

        Args:
            src: source path on the system file system.
            dst_directory: workspace receiving the copy: "result" or "tmp".
        """
        destination = workspace.resolve(dst_directory, ".")
        # Expressed as an absolute virtual path so that it stays local even if
        # the agent navigated into reMarkable.
        count = ctx.files.copy(src, virtual.local_to_virtual(str(destination)), recursive=True)
        return ToolResult.ok(f"Copied {count} file(s) from {src} to {workspace.describe(destination)}.")

    return [
        workspace_file_list,
        workspace_file_read,
        workspace_file_write,
        workspace_file_delete,
        workspace_file_move,
        workspace_file_get,
    ]
