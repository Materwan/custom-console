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
        """Convert a local PDF into a Markdown file, keeping formulas and tables, then read
        the Markdown with file_system_read. To convert a PDF from the reMarkable, copy it
        to a local folder first.

        Args:
            path: the PDF file.
            output_path: Markdown file to create (default: next to the PDF, with a .md extension).
            pages: 0-based page numbers to convert (default: all pages).
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
        """Convert a reMarkable .rmdoc document into a PDF. A document copied from the
        reMarkable arrives as an .rmdoc file: use this to get its PDF (the original PDF,
        with my handwriting and highlights drawn on it; a notebook becomes handwritten pages).
        Then read the PDF with pdf_to_markdown if you need its text.

        Args:
            path: the local .rmdoc file (copy it from the reMarkable first).
            output_path: PDF file to create (default: next to the .rmdoc, with a .pdf extension).
            include_handwriting: draw the handwriting on the pages (default); false extracts the original PDF untouched.
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
