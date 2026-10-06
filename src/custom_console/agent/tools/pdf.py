"""Document conversion: PDF to Markdown, reMarkable .rmdoc to PDF."""

import os
from typing import Callable, List, Optional

from ...fs.rmdoc import default_output, rmdoc_to_pdf as convert_rmdoc
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded, zone_level
from .filesystem import TextFile, save_text


def convert_pdf(pdf_path: str, pages: Optional[List[int]] = None) -> str:
    """Markdown text of a PDF, math and tables included (pymupdf4llm)."""
    try:
        import pymupdf4llm
    except ImportError as error:
        raise ImportError(
            "PDF conversion needs the optional dependency: pip install 'custom-console[pdf]'"
        ) from error
    kwargs = {"pages": pages} if pages else {}
    return pymupdf4llm.to_markdown(pdf_path, **kwargs)


def pdf_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    files = ctx.files

    @guarded(ctx, zone_level(ctx, PermissionLevel.WRITE, "path", "output_path"))
    def pdf_to_markdown(
        path: str,
        output_path: Optional[str] = None,
        pages: Optional[List[int]] = None,
    ) -> ToolResult:
        """Convert a local PDF into a Markdown file (formulas and tables kept), then read it with file_system_read. Copy a reMarkable PDF to a local folder first.

        Args:
            path: the PDF
            output_path: Markdown file (default: next to the PDF)
            pages: 0-based page numbers (default: all)
        """
        source = files.local_path(path, "pdf_to_markdown")
        if not os.path.isfile(source):
            raise FileNotFoundError(f"{path} is not a file.")
        if not source.lower().endswith(".pdf"):
            raise ValueError(f"{path} is not a PDF file.")

        markdown = convert_pdf(source, pages)
        if output_path:
            target = files.local_path(output_path, "pdf_to_markdown")
        else:
            target = os.path.splitext(source)[0] + ".md"
        ctx.snapshot(target)
        save_text(target, TextFile(markdown))
        ctx.reads.mark(target)
        return ToolResult.ok({"output": target, "characters": len(markdown)})

    @guarded(ctx, zone_level(ctx, PermissionLevel.WRITE, "path", "output_path"))
    def rmdoc_to_pdf(
        path: str,
        output_path: Optional[str] = None,
        include_handwriting: bool = True,
    ) -> ToolResult:
        """Convert a reMarkable .rmdoc (copy it locally first) into a PDF with my handwriting drawn on it. Read its text with pdf_to_markdown.

        Args:
            path: the local .rmdoc
            output_path: PDF to create (default: next to it)
            include_handwriting: false extracts the original PDF untouched
        """
        source = files.local_path(path, "rmdoc_to_pdf")
        target = files.local_path(output_path, "rmdoc_to_pdf") if output_path else default_output(source)
        ctx.snapshot(target)
        result = convert_rmdoc(source, target, handwriting=include_handwriting, overwrite=True)
        return ToolResult.ok(
            {
                "output": result.output,
                "type": result.file_type,
                "pages": result.pages,
                "pages_with_handwriting": result.annotated_pages,
                "notes": result.notes,
            }
        )

    return [pdf_to_markdown, rmdoc_to_pdf]
