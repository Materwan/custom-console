"""`file_system_*` tools: explore the virtual file system (disks, WSL, reMarkable)."""

import ast
from typing import Callable, List, Literal, Optional

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, clip, guarded, truncation_notice

MAX_READ_BYTES = 1_000_000
SUMMARY_LINES = 50


def python_outline(source: str) -> str:
    """Classes, functions and methods of a Python source, with line numbers."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return f"Syntax error, cannot outline: {error}"

    lines: List[str] = []

    def signature(node) -> str:
        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args)]
        if args.vararg:
            names.append("*" + args.vararg.arg)
        names += [a.arg for a in args.kwonlyargs]
        if args.kwarg:
            names.append("**" + args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(names)})"

    def doc(node) -> str:
        text = ast.get_docstring(node)
        return f"  # {text.splitlines()[0]}" if text else ""

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            lines.append(f"class {node.name}({bases}):  [L{node.lineno}]{doc(node)}")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"    {signature(item)}  [L{item.lineno}]{doc(item)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"{signature(node)}  [L{node.lineno}]{doc(node)}")
    return "\n".join(lines) or "(no class or function)"


def filesystem_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    files = ctx.files

    @guarded(ctx, PermissionLevel.NONE)
    def file_system_pwd() -> ToolResult:
        """Return the current working directory (a local path, "/" or "reMarkable:/...")."""
        return ToolResult.ok(files.location)

    @guarded(ctx, PermissionLevel.READ)
    def file_system_list(path: str = ".", show_hidden: bool = False) -> ToolResult:
        """List a directory. Sub-directories end with "/".

        Args:
            path: directory to list (relative to the working directory, or absolute).
            show_hidden: include entries starting with a dot.
        """
        return ToolResult.ok(files.listdir(path, show_hidden))

    @guarded(ctx, PermissionLevel.READ)
    def file_system_read(
        path: str,
        mode: Literal["full", "range", "summary"] = "full",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_chars: int = 20000,
    ) -> ToolResult:
        """Read a text file.

        Args:
            path: file to read.
            mode: "full" = whole file; "range" = lines start_line..end_line (1-based,
                inclusive); "summary" = outline of a Python file (classes/functions),
                or the first 50 lines of any other file.
            start_line: first line for "range".
            end_line: last line for "range".
            max_chars: the answer is truncated beyond this many characters.
        """
        read = files.read(path, max_bytes=MAX_READ_BYTES)
        text = read.text

        if mode == "range":
            if start_line is None or end_line is None:
                raise ValueError("start_line and end_line are required in range mode.")
            if start_line < 1 or end_line < start_line:
                raise ValueError("Require 1 <= start_line <= end_line.")
            text = "\n".join(text.splitlines()[start_line - 1 : end_line])
        elif mode == "summary":
            if path.lower().endswith(".py"):
                text = python_outline(text)
            else:
                text = "\n".join(text.splitlines()[:SUMMARY_LINES])
        elif mode != "full":
            raise ValueError(f"Unknown mode {mode!r}.")

        text, truncated = clip(text, max_chars)
        if truncated:
            text += truncation_notice(max_chars)
        if read.truncated:
            text += f"\n[... file larger than {MAX_READ_BYTES} bytes, only the start was read ...]"
        return ToolResult.ok(text)

    @guarded(ctx, PermissionLevel.READ)
    def file_system_stat(path: str) -> ToolResult:
        """Return the readable/writable/executable permissions of a path."""
        return ToolResult.ok(files.stat(path)._asdict())

    @guarded(ctx, PermissionLevel.READ)
    def file_system_find(
        pattern: str, path: str = ".", depth: int = 2, strict: bool = False
    ) -> ToolResult:
        """Find files and directories by name.

        Args:
            pattern: case-insensitive regex on the entry name; a trailing "/" restricts
                the search to directories.
            path: directory to search in.
            depth: how many levels to go down (1 = direct children only).
            strict: the whole name must match the pattern.
        """
        return ToolResult.ok(files.find(pattern, path, depth, strict))

    @guarded(ctx, PermissionLevel.READ)
    def file_system_cd(path: str) -> ToolResult:
        """Change the working directory (also switches between drives, WSL and reMarkable)."""
        files.change_directory(path)
        return ToolResult.ok(f"Changed directory to {files.location}")

    @guarded(ctx, PermissionLevel.READ)
    def file_system_tree(path: str = ".", depth: int = 2, show_hidden: bool = False) -> ToolResult:
        """Show the tree of a directory.

        Args:
            path: directory to show.
            depth: levels to expand (-1 = unlimited).
            show_hidden: include entries starting with a dot.
        """
        return ToolResult.ok("\n".join(files.tree(path, depth, show_hidden)))

    @guarded(ctx, PermissionLevel.WRITE)
    def file_system_copy(src: str, dst: str, recursive: bool = False) -> ToolResult:
        """Copy a file or directory (local, WSL, reMarkable).

        Args:
            src: source path.
            dst: destination path; an existing directory receives the copy. From
                reMarkable, `dst` is the local destination folder.
            recursive: required to copy a directory.
        """
        count = files.copy(src, dst, recursive)
        return ToolResult.ok(f"Copied {count} file(s) from {src} to {dst}.")

    return [
        file_system_pwd,
        file_system_list,
        file_system_read,
        file_system_stat,
        file_system_find,
        file_system_cd,
        file_system_tree,
        file_system_copy,
    ]
