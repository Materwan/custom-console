"""`file_system_*` tools: explore and change files (disks, WSL, reMarkable).

Inside the free zone (the folder the agent was started in and its subfolders)
every one of these tools runs without asking, but for changes to its protected
paths (see `zone`). Outside it they keep their usual level: reads are level 1,
anything that changes something is level 2.

Searches (glob, grep) list the files of a git repository with git itself, so
what .gitignore leaves out (builds, caches, virtual environments) is not searched.
"""

import ast
import codecs
import difflib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple

from ...fs import Backend, BinaryFileError
from ..diffs import clip_diff, count_changes, make_diff, new_file_diff
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, clip, guarded, truncation_notice, zone_level

MAX_READ_BYTES = 1_000_000
SUMMARY_LINES = 50
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_WALKED_ENTRIES = 100_000
MAX_LINE_CHARS = 300
DIFF_LINES_SHOWN = 300  # folded under the tool's line until the user unfolds it
DIFF_LINES_ASKED = 14
MAX_EDITS = 50  # replacements in one file_system_multi_edit
CLOSEST_MIN_RATIO = 0.6  # how alike a passage must be to be shown when old_text is not found
CLOSEST_MAX_FILE_LINES = 20_000
CLOSEST_MAX_TEXT_LINES = 200
GIT_LIST_TIMEOUT = 15

# Folders that only contain noise for glob and grep.
SKIPPED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


# --------------------------------------------------------------------------- #
# Text files
# --------------------------------------------------------------------------- #


@dataclass
class TextFile:
    text: str  # always with "\n" line endings
    newline: str = "\n"  # what the file used, restored when saving
    bom: bool = False


def load_text(path: str) -> TextFile:
    """A UTF-8 text file. Refuses binary and non-UTF-8 files (changing those
    through a text tool would corrupt them)."""
    with open(path, "rb") as handle:
        data = handle.read()
    if b"\x00" in data[:8192]:
        raise BinaryFileError(f"{path}: binary file")
    bom = data.startswith(codecs.BOM_UTF8)
    try:
        text = data.decode("utf-8-sig" if bom else "utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8: refusing to change it as text") from None
    return TextFile(text.replace("\r\n", "\n"), "\r\n" if "\r\n" in text else "\n", bom)


def save_text(path: str, file: TextFile) -> None:
    data = file.text.replace("\r\n", "\n").replace("\n", file.newline).encode("utf-8")
    if file.bom:
        data = codecs.BOM_UTF8 + data
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def closest_passage(text: str, old: str) -> str:
    """The passage of `text` most like `old` (as many lines), with its line numbers, to show the model
    what it should have copied; "" when nothing is alike enough (or the file is too long to look)."""
    lines, wanted = text.split("\n"), old.strip("\n").split("\n")
    size = len(wanted)
    if not old.strip() or size > CLOSEST_MAX_TEXT_LINES or len(lines) > CLOSEST_MAX_FILE_LINES:
        return ""
    target = "\n".join(line.strip() for line in wanted)
    best, where = 0.0, -1
    for start in range(max(1, len(lines) - size + 1)):
        window = "\n".join(line.strip() for line in lines[start : start + size])
        matcher = difflib.SequenceMatcher(None, window, target, autojunk=False)
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue
        ratio = matcher.ratio()
        if ratio > best:
            best, where = ratio, start
    if where < 0 or best < CLOSEST_MIN_RATIO:
        return ""
    shown = "\n".join(f"{where + 1 + i:>6}\t{line}" for i, line in enumerate(lines[where : where + size]))
    return f"\nThe closest passage (lines {where + 1}-{where + size}, {best:.0%} alike; the numbers are not part of the file):\n{shown}"


def _match_trailing_spaces(text: str, old: str) -> Optional[Tuple[int, int]]:
    """Where `old` is in `text` as whole lines when trailing spaces are ignored, if exactly once."""
    lines, wanted = text.split("\n"), [line.rstrip() for line in old.split("\n")]
    size = len(wanted)
    found = [
        start
        for start in range(len(lines) - size + 1)
        if [line.rstrip() for line in lines[start : start + size]] == wanted
    ]
    if len(found) != 1:
        return None
    begin = sum(len(line) + 1 for line in lines[: found[0]])
    return begin, begin + len("\n".join(lines[found[0] : found[0] + size]))


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> Tuple[str, int]:
    """`text` with `old` replaced by `new`, and the number of replacements.

    Without `replace_all`, `old` must occur exactly once: the model has to give
    enough context to say which occurrence it means. Spaces at the end of lines
    are not required to match (the model rarely sees them); anything else is,
    and when nothing matches the error shows the closest passage.
    """
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old:
        raise ValueError("old_text must not be empty (to create a file use file_system_write).")
    if old == new:
        raise ValueError("old_text and new_text are identical: nothing to change.")
    count = text.count(old)
    if count == 0:
        relaxed = _match_trailing_spaces(text, old)
        if relaxed is not None:
            begin, end = relaxed
            return text[:begin] + new + text[end:], 1
        hint = " Check the indentation and whitespace." if old.strip() and old.strip() in text else ""
        raise ValueError(
            "old_text was not found in the file." + hint + " Copy the text exactly as it is in the file."
            + closest_passage(text, old)
        )
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_text appears {count} times. Include more surrounding lines to make it unique, "
            "or set replace_all=true to replace every occurrence."
        )
    if replace_all:
        return text.replace(old, new), count
    return text.replace(old, new, 1), 1


def apply_edits(text: str, edits: List[Dict[str, Any]]) -> Tuple[str, int]:
    """`text` with every edit applied in order (each on the result of the previous), and the total
    number of replacements. Raises on the first edit that cannot be made, naming it."""
    if not edits:
        raise ValueError("No edit given.")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"At most {MAX_EDITS} edits at once.")
    total = 0
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise ValueError(f"Edit {number}: expected an object with old_text and new_text, got {edit!r}.")
        try:
            text, count = apply_edit(
                text,
                str(edit.get("old_text", "")),
                str(edit.get("new_text", "")),
                bool(edit.get("replace_all", False)),
            )
        except ValueError as error:
            raise ValueError(f"Edit {number} of {len(edits)}: {error} Nothing was changed.") from None
        total += count
    return text, total


# --------------------------------------------------------------------------- #
# Walking and matching
# --------------------------------------------------------------------------- #


def git_listing(root: str) -> Optional[List[str]]:
    """The files below `root` that git does not ignore (tracked, or new and not in .gitignore), relative
    to `root` with "/" separators; None when `root` is not in a git repository (or git is missing)."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(
            [git, "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=GIT_LIST_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return sorted({path for path in done.stdout.decode("utf-8", errors="replace").split("\0") if path})


def walk_entries(root: str, is_cancelled: Callable[[], bool] = lambda: False) -> Iterator[Tuple[str, str, bool]]:
    """``(relative path, absolute path, is_dir)`` of everything below `root`,
    with "/" separators, skipping folders that are only noise; in a git repository,
    only what git does not ignore."""
    listed = git_listing(root)
    if listed is not None:
        yield from _git_entries(root, listed, is_cancelled)
        return
    yield from _walk_disk(root, is_cancelled)


def _git_entries(root: str, files: List[str], is_cancelled: Callable[[], bool]) -> Iterator[Tuple[str, str, bool]]:
    folders = set()
    kept = []
    for relative in files:
        parts = relative.split("/")
        if any(part in SKIPPED_DIRS for part in parts[:-1]):
            continue
        kept.append(relative)
        folders.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    for count, relative in enumerate(sorted(folders)):
        if count > MAX_WALKED_ENTRIES or is_cancelled():
            return
        yield relative, os.path.join(root, *relative.split("/")), True
    for count, relative in enumerate(kept):
        if count > MAX_WALKED_ENTRIES or is_cancelled():
            return
        absolute = os.path.join(root, *relative.split("/"))
        if os.path.lexists(absolute):  # a tracked file deleted since the last commit is not there
            yield relative, absolute, False


def _walk_disk(root: str, is_cancelled: Callable[[], bool]) -> Iterator[Tuple[str, str, bool]]:
    seen = 0
    for current, dirs, names in os.walk(root):
        if is_cancelled():
            return
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
        relative = os.path.relpath(current, root)
        prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        for name in dirs:
            yield prefix + name, os.path.join(current, name), True
        for name in sorted(names):
            yield prefix + name, os.path.join(current, name), False
        seen += len(dirs) + len(names)
        if seen > MAX_WALKED_ENTRIES:
            return


def glob_regex(pattern: str) -> "re.Pattern[str]":
    """Regex for a glob: ``*`` and ``?`` stay inside a folder, ``**`` crosses
    folders. A pattern without "/" matches names at any depth."""
    pattern = pattern.replace("\\", "/").rstrip("/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    out: List[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern[i : i + 2] == "**":
            i += 2
            if pattern[i : i + 1] == "/":
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and pattern.find("]", i + 2) != -1:
            end = pattern.find("]", i + 2)
            out.append(pattern[i : end + 1].replace("[!", "[^"))
            i = end
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def grep_file(
    regex: "re.Pattern[str]", lines: List[str], context: int
) -> List[Tuple[int, str, bool]]:
    """``(line number, text, is_match)`` of the matches and their context lines."""
    matched = [index for index, line in enumerate(lines) if regex.search(line)]
    if not context:
        return [(index + 1, lines[index], True) for index in matched]
    wanted = {}
    for index in matched:
        for near in range(max(0, index - context), min(len(lines), index + context + 1)):
            wanted[near] = wanted.get(near, False) or near == index
    return [(index + 1, lines[index], is_match) for index, is_match in sorted(wanted.items())]


# --------------------------------------------------------------------------- #
# Python outline (used by file_system_read "summary")
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# The tools
# --------------------------------------------------------------------------- #


def filesystem_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    files = ctx.files
    READ, WRITE = PermissionLevel.READ, PermissionLevel.WRITE

    def reading(*params: str):
        return zone_level(ctx, READ, *params)

    def writing(*params: str, strict: Tuple[str, ...] = ()):
        return zone_level(ctx, WRITE, *params, strict=strict)

    # -- what the user is asked when a change happens outside the zone ------------ #

    def preview_write(path: str, content: str, overwrite: bool = True) -> str:
        local = files.local_path(path, "write")
        content = content.replace("\r\n", "\n")
        if os.path.isfile(local):
            diff = make_diff(load_text(local).text, content, path)
            what = f"overwrite {path}"
        else:
            diff = new_file_diff(content)
            what = f"create {path}"
        return f"Agent wants to {what}:\n{clip_diff(diff, DIFF_LINES_ASKED)}"

    def preview_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        local = files.local_path(path, "edit")
        current = load_text(local).text
        edited, _count = apply_edit(current, old_text, new_text, replace_all)
        return f"Agent wants to edit {path}:\n{clip_diff(make_diff(current, edited, path), DIFF_LINES_ASKED)}"

    def preview_edits(path: str, edits: List[Dict[str, Any]]) -> str:
        local = files.local_path(path, "edit")
        current = load_text(local).text
        edited, _count = apply_edits(current, edits)
        return f"Agent wants to edit {path}:\n{clip_diff(make_diff(current, edited, path), DIFF_LINES_ASKED)}"

    def change_file(path: str, change: Callable[[str], Tuple[str, int]]) -> ToolResult:
        """Read `path`, apply `change` to its text, save it (line endings and BOM kept) and report."""
        local = files.local_path(path, "edit")
        if not os.path.isfile(local):
            raise FileNotFoundError(f"{path} is not a file.")
        ctx.reads.check(local)

        current = load_text(local)
        edited, count = change(current.text)
        ctx.snapshot(local)
        save_text(local, TextFile(edited, current.newline, current.bom))
        ctx.reads.mark(local)

        diff = make_diff(current.text, edited, path)
        added, removed = count_changes(diff)
        return ToolResult.ok(
            {"path": local, "replacements": count, "lines_added": added, "lines_removed": removed},
            diff=clip_diff(diff, DIFF_LINES_SHOWN),
        )

    @guarded(ctx, reading("path"))
    def file_system_list(path: str = ".", show_hidden: bool = False) -> ToolResult:
        """List a directory ("/" suffix = folder).

        Args:
            path: directory (relative or absolute)
            show_hidden: include dot entries
        """
        return ToolResult.ok(files.listdir(path, show_hidden))

    @guarded(ctx, reading("path"))
    def file_system_cd(path: str) -> ToolResult:
        """Change the working directory (also switches drive, WSL, reMarkable).

        Args:
            path: the folder to move into
        """
        files.change_directory(path)
        return ToolResult.ok(f"Changed directory to {files.location}")

    @guarded(ctx, reading("path"))
    def file_system_tree(path: str = ".", depth: int = 2, show_hidden: bool = False) -> ToolResult:
        """Show the tree of a directory.

        Args:
            path: directory
            depth: levels (-1 = unlimited)
            show_hidden: include dot entries
        """
        return ToolResult.ok("\n".join(files.tree(path, depth, show_hidden)))

    @guarded(ctx, reading("path"))
    def file_system_stat(path: str) -> ToolResult:
        """Permissions (r/w/x) of a path.

        Args:
            path: file or folder
        """
        return ToolResult.ok(files.stat(path)._asdict())

    # -- search ---------------------------------------------------------------- #

    @guarded(ctx, reading("path"))
    def file_system_find(pattern: str, path: str = ".", depth: int = 2, strict: bool = False) -> ToolResult:
        """Find entries by name (also on the reMarkable).

        Args:
            pattern: case-insensitive regex on the name; trailing "/" = folders only
            path: directory to search
            depth: levels (1 = direct children)
            strict: the whole name must match
        """
        return ToolResult.ok(files.find(pattern, path, depth, strict))

    @guarded(ctx, reading("path"))
    def file_system_glob(pattern: str, path: str = ".", max_results: int = 200) -> ToolResult:
        """Find local files by glob, newest first.

        Args:
            pattern: e.g. "**/*.py"; without "/" it matches names at any depth; trailing "/" = folders only
            path: directory to search
            max_results: maximum number of paths
        """
        root = files.local_path(path, "glob")
        if not os.path.isdir(root):
            raise NotADirectoryError(f"{path} is not a directory.")
        directories = pattern.rstrip().endswith(("/", "\\"))
        regex = glob_regex(pattern)
        found = [
            (relative + ("/" if is_dir else ""), absolute)
            for relative, absolute, is_dir in walk_entries(root, ctx.is_cancelled)
            if is_dir == directories and regex.match(relative)
        ]
        found.sort(key=lambda item: _mtime(item[1]), reverse=True)
        if not found:
            return ToolResult.ok("No match.")
        shown = "\n".join(relative for relative, _ in found[:max_results])
        if len(found) > max_results:
            shown += f"\n[... {len(found) - max_results} more ...]"
        return ToolResult.ok(shown)

    @guarded(ctx, reading("path"))
    def file_system_grep(
        pattern: str,
        path: str = ".",
        include: Optional[str] = None,
        ignore_case: bool = False,
        context: int = 0,
        max_matches: int = 100,
    ) -> ToolResult:
        """Regex search in local text files. Output "path:LINE:text" ("-" for context lines).

        Args:
            pattern: regex (Python syntax)
            path: file or directory (recursive)
            include: glob filter, e.g. "*.py"
            ignore_case: case-insensitive
            context: lines around each match
            max_matches: stop after this many matching lines
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as error:
            raise ValueError(f"Invalid regular expression: {error}") from None
        root = files.local_path(path, "grep")
        if os.path.isfile(root):
            candidates = iter([(os.path.basename(root), root, False)])
        else:
            candidates = walk_entries(root, ctx.is_cancelled)
        include_regex = glob_regex(include) if include else None
        context = max(0, min(context, 10))

        output: List[str] = []
        matches = 0
        truncated = False
        for relative, absolute, is_dir in candidates:
            if is_dir or (include_regex and not include_regex.match(relative)):
                continue
            try:
                if os.path.getsize(absolute) > MAX_SEARCH_FILE_BYTES:
                    continue
                with open(absolute, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
            previous = 0
            for number, text, is_match in grep_file(regex, lines, context):
                if is_match:
                    if matches >= max_matches:
                        truncated = True
                        break
                    matches += 1
                if context and previous and number > previous + 1:
                    output.append("--")
                previous = number
                shown = text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + "…"
                output.append(f"{relative}{':' if is_match else '-'}{number}{':' if is_match else '-'}{shown}")
            if truncated:
                break
        if not output:
            return ToolResult.ok("No match.")
        if truncated:
            output.append(f"[... stopped after {max_matches} matches ...]")
        return ToolResult.ok("\n".join(output))

    # -- reading ---------------------------------------------------------------- #

    @guarded(ctx, reading("path"))
    def file_system_read(
        path: str,
        mode: Literal["full", "range", "summary"] = "full",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_chars: int = 20000,
        line_numbers: bool = False,
    ) -> ToolResult:
        """Read a text file; read a file before editing it. A long file is cut and the answer says how to continue with mode "range".

        Args:
            path: file to read
            mode: "full", "range" (start_line..end_line, 1-based) or "summary" (Python outline, else the first 50 lines)
            start_line: first line for "range"
            end_line: last line for "range" (default: the end)
            max_chars: the answer is cut beyond this
            line_numbers: prefix each line with its number (not part of the file: never copy it into an edit)
        """
        read = files.read(path, max_bytes=MAX_READ_BYTES)
        all_lines = read.text.split("\n")
        first = 1

        if mode == "range":
            if start_line is None:
                raise ValueError("start_line is required in range mode.")
            end = len(all_lines) if end_line is None else end_line
            if start_line < 1 or end < start_line:
                raise ValueError("Require 1 <= start_line <= end_line.")
            if start_line > len(all_lines):
                raise ValueError(f"The file has only {len(all_lines)} lines.")
            first, lines = start_line, all_lines[start_line - 1 : end]
        elif mode == "summary":
            lines = python_outline(read.text).split("\n") if path.lower().endswith(".py") else all_lines[:SUMMARY_LINES]
        elif mode == "full":
            lines = all_lines
        else:
            raise ValueError(f"Unknown mode {mode!r}.")

        numbered = line_numbers and mode != "summary"
        shown = [f"{first + i:>6}\t{line}" for i, line in enumerate(lines)] if numbered else lines
        limit = max(1, min(int(max_chars), ctx.max_chars()))
        text, truncated = clip("\n".join(shown), limit)
        if truncated:
            whole = text.count("\n")  # lines shown entirely
            text = text[: text.rfind("\n")] if whole else text
            if mode == "summary":
                text += truncation_notice(limit)
            else:
                last = first + max(whole, 1) - 1
                text += (
                    f"\n[... cut at {limit} characters: lines {first}-{last} of {len(all_lines)} shown. "
                    f'Read on with mode="range", start_line={last + 1} ...]'
                )
        if read.truncated:
            text += f"\n[... file larger than {MAX_READ_BYTES} bytes, only the start was read ...]"
        try:
            local = files.local_path(path, "read")
        except Exception:  # the reMarkable: no local file to follow
            local = None
        if local is not None:
            if not read.truncated and not truncated:
                ctx.reads.mark(local, complete=mode == "full")  # it may edit the file now
            else:
                ctx.reads.saw_part(local)  # not editable yet, but it is told when the file changes
        return ToolResult.ok(text)

    # -- changing files ----------------------------------------------------------- #

    @guarded(ctx, writing("path"), describe=preview_write)
    def file_system_write(path: str, content: str, overwrite: bool = True) -> ToolResult:
        """Create or replace a whole text file (creates folders; an existing file must have been read first). To change part of a file prefer file_system_edit.

        Args:
            path: file to write
            content: the complete new content
            overwrite: false = fail instead of replacing
        """
        local = files.local_path(path, "write")
        if os.path.isdir(local):
            raise IsADirectoryError(f"{path} is a directory.")
        content = content.replace("\r\n", "\n")

        previous: Optional[TextFile] = None
        exists = os.path.lexists(local)
        if exists:
            if not overwrite:
                raise FileExistsError(f"{path} already exists.")
            ctx.reads.check(local)
            try:
                previous = load_text(local)
            except (BinaryFileError, ValueError):
                previous = None  # replacing a binary file: no diff to show
        ctx.snapshot(local)

        save_text(local, TextFile(content, previous.newline if previous else "\n", previous.bom if previous else False))
        ctx.reads.mark(local)

        diff = make_diff(previous.text, content, path) if previous else new_file_diff(content)
        added, removed = count_changes(diff)
        return ToolResult.ok(
            {"path": local, "created": not exists, "lines_added": added, "lines_removed": removed},
            diff=clip_diff(diff, DIFF_LINES_SHOWN),
        )

    @guarded(ctx, writing("path"), describe=preview_edit)
    def file_system_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
        """Replace exact text in a file. The file must have been read first. old_text must match exactly (indentation included) and be unique unless replace_all; if it is not found the error shows the closest passage.

        Args:
            path: file to edit
            old_text: the exact text to replace; add surrounding lines to make it unique
            new_text: the replacement
            replace_all: replace every occurrence
        """
        return change_file(path, lambda text: apply_edit(text, old_text, new_text, replace_all))

    @guarded(ctx, writing("path"), describe=preview_edits)
    def file_system_multi_edit(path: str, edits: List[Dict[str, Any]]) -> ToolResult:
        """Several file_system_edit replacements in one file, applied in order: all or none.

        Args:
            path: file to edit (read it first)
            edits: the replacements, each {"old_text", "new_text", "replace_all"}
        """
        return change_file(path, lambda text: apply_edits(text, edits))

    @guarded(ctx, writing("src", "dst"))
    def file_system_copy(src: str, dst: str, recursive: bool = False) -> ToolResult:
        """Copy a file or folder (local, WSL, reMarkable).

        Args:
            src: source
            dst: destination; an existing folder receives the copy
            recursive: required for a folder
        """
        source, destination = files.resolve(src), files.resolve(dst)
        if source.backend is Backend.LOCAL and destination.backend is Backend.LOCAL:
            into = os.path.isdir(destination.path)
            ctx.snapshot(os.path.join(destination.path, os.path.basename(source.path)) if into else destination.path)
        count = files.copy(src, dst, recursive)
        return ToolResult.ok(f"Copied {count} file(s) from {src} to {dst}.")

    @guarded(ctx, writing("src", "dst", strict=("src",)))
    def file_system_move(src: str, dst: str) -> ToolResult:
        """Move or rename a local file or folder; an existing file is never overwritten.

        Args:
            src: what to move
            dst: new path, or the folder to move it into
        """
        origin = files.local_path(src, "mv")
        final = files.move(src, dst)
        if ctx.checkpoints is not None:
            ctx.checkpoints.moved(origin, final)
        return ToolResult.ok(f"Moved {src} to {final}.")

    @guarded(ctx, writing("path", strict=("path",)))
    def file_system_remove(path: str, recursive: bool = False) -> ToolResult:
        """Delete a file or folder. Refuses drive roots, the home folder and any folder containing the current directory.

        Args:
            path: what to delete
            recursive: needed for a non-empty folder
        """
        files.check_removable(path)
        target = files.resolve(path)
        if target.backend is Backend.LOCAL:
            ctx.snapshot(target.path)
        files.remove(path, recursive)
        return ToolResult.ok(f"Removed {path}.")

    return [
        file_system_list,
        file_system_cd,
        file_system_tree,
        file_system_stat,
        file_system_find,
        file_system_glob,
        file_system_grep,
        file_system_read,
        file_system_write,
        file_system_edit,
        file_system_multi_edit,
        file_system_copy,
        file_system_move,
        file_system_remove,
    ]
