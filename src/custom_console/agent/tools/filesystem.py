"""`file_system_*` tools: explore the virtual file system (disks, WSL, reMarkable)."""

import ast
from typing import Callable, List, Literal, Optional

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, cap_items, clip, guarded, truncation_notice

MAX_READ_BYTES = 1_000_000
MAX_READ_CHARS = 8000  # default cut of a read: the rest is one `range` call away
MAX_ENTRIES = 150  # entries of a list/find/tree answer
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
        """Current directory."""
        return ToolResult.ok(files.location)

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def file_system_list(path: str = ".", show_hidden: bool = False) -> ToolResult:
        """List a directory ("/" suffix = folder)."""
        return ToolResult.ok(cap_items(files.listdir(path, show_hidden), MAX_ENTRIES))

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def file_system_read(
        path: str,
        mode: Literal["full", "range", "summary"] = "full",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_chars: int = MAX_READ_CHARS,
    ) -> ToolResult:
        """Read a text file. "range" needs start_line/end_line (1-based); "summary" = Python
        outline, else the first 50 lines. Long output is cut: read the rest with "range"."""
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
            text += truncation_notice(max_chars, 'use mode="range"')
        if read.truncated:
            text += f"\n[file > {MAX_READ_BYTES} bytes: only the start was read]"
        return ToolResult.ok(text)

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def file_system_stat(path: str) -> ToolResult:
        """Permissions (r/w/x) of a path."""
        return ToolResult.ok(files.stat(path)._asdict())

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def file_system_find(
        pattern: str, path: str = ".", depth: int = 2, strict: bool = False
    ) -> ToolResult:
        """Find entries by name: case-insensitive regex (trailing "/" = folders only);
        strict = the whole name must match; depth 1 = direct children."""
        return ToolResult.ok(cap_items(list(files.find(pattern, path, depth, strict)), MAX_ENTRIES))

    @guarded(ctx, PermissionLevel.READ)
    def file_system_cd(path: str) -> ToolResult:
        """Change directory (also switches drive, WSL, reMarkable)."""
        files.change_directory(path)
        ctx.memo.clear()  # relative paths now mean something else
        return ToolResult.ok(f"Changed directory to {files.location}")

    @guarded(ctx, PermissionLevel.READ, memo=True)
    def file_system_tree(path: str = ".", depth: int = 2, show_hidden: bool = False) -> ToolResult:
        """Tree of a directory (depth -1 = unlimited)."""
        return ToolResult.ok("\n".join(cap_items(files.tree(path, depth, show_hidden), MAX_ENTRIES * 2, "lower the depth")))

    @guarded(ctx, PermissionLevel.WRITE)
    def file_system_copy(src: str, dst: str, recursive: bool = False) -> ToolResult:
        """Copy a file or folder (any disk, WSL, reMarkable). `dst`: an existing folder receives
        the copy; recursive is required for a folder."""
        count = files.copy(src, dst, recursive)
        return ToolResult.ok(f"copied {count} file(s)")

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
