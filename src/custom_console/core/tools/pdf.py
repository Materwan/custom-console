"""`local_pdf_to_markdown`: the text of a PDF as Markdown, through the optional `pdf` extra."""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

from ..permissions.engine import READ, CallInfo, PathUse
from ..vfs.backend import VfsError
from .base import STR, ToolContext, ToolError, ToolSpec, cut_middle, obj
from .files import _resolve, read_stat


def parse_pages(spec: str, total: int | None = None) -> list[int]:
    """`1-3,5` -> zero-based page numbers `[0, 1, 2, 4]`. Raises ToolError on nonsense."""
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        low, dash, high = part.partition("-")
        try:
            first = int(low)
            last = int(high) if dash else first
        except ValueError:
            raise ToolError(f"Pages must look like 1-3,5 (not {spec!r}).") from None
        if first < 1 or last < first:
            raise ToolError(f"Pages must look like 1-3,5 (not {spec!r}).")
        pages += range(first - 1, last)
    if total is not None:
        pages = [p for p in pages if p < total]
    return list(dict.fromkeys(pages))


def _available(ctx: ToolContext) -> str:
    """Whether the package is installed, without importing it: importing pymupdf takes a second and a half, and this runs
    on the interface's thread every time the tool list is built."""
    if importlib.util.find_spec("pymupdf4llm") is None:
        return "The pdf extra is not installed: uv tool install 'custom-console[pdf]'."
    return ""


def _inspect(ctx: ToolContext, args: dict[str, Any]) -> CallInfo:
    return CallInfo("local_pdf_to_markdown", READ, (PathUse(args.get("path", "")),), summary=f"Convert {args.get('path', '')}")


def _run(ctx: ToolContext, args: dict[str, Any]) -> str:
    reason = _available(ctx)
    if reason:
        raise ToolError(reason)
    target = _resolve(ctx, args["path"])
    if not target.on_disk:
        raise ToolError("Copy the file to a disk folder first (cp), then convert it.")
    info = read_stat(ctx, target)
    if info.is_dir:
        raise ToolError(f"{target.virtual} is a folder.")
    pymupdf4llm = importlib.import_module("pymupdf4llm")
    pages = parse_pages(str(args["pages"])) if args.get("pages") else None
    try:
        text = pymupdf4llm.to_markdown(target.real(), pages=pages)
    except (VfsError, OSError) as error:
        raise ToolError(f"{target.virtual}: {error}") from None
    except Exception as error:
        raise ToolError(f"{target.virtual} could not be converted ({type(error).__name__}: {str(error)[:200]}).") from None
    return cut_middle(str(text), ctx.result_limit) if str(text).strip() else "No text found (a scanned PDF has none)."


PDF_TOOL = ToolSpec(
    "local_pdf_to_markdown",
    "Convert a PDF file to Markdown text (headings, lists, tables). `pages` selects pages, e.g. `1-3,5`.",
    obj({"path": STR, "pages": {**STR, "description": "Pages to convert, e.g. 1-3,5 (default: all)"}}, ["path"]),
    READ, _inspect, _run, read_only_for_subagents=True, available=_available)

__all__ = ["PDF_TOOL", "parse_pages"]
