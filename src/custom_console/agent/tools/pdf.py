"""PDF to Markdown conversion for files of the agent workspace."""

from typing import Callable, List, Optional

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded


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
    workspace = ctx.workspace

    @guarded(ctx, PermissionLevel.WRITE)
    def pdf_to_markdown(
        directory: str,
        path: str,
        output_path: Optional[str] = None,
        pages: Optional[List[int]] = None,
    ) -> ToolResult:
        """Convert a PDF of a workspace into a Markdown file, keeping formulas and tables.

        Use `workspace_file_get` first to bring a PDF from elsewhere into a workspace,
        then read the produced Markdown with `workspace_file_read`.

        Args:
            directory: workspace holding the PDF: "result" or "tmp".
            path: PDF file inside that workspace.
            output_path: Markdown file to create in the same workspace
                (default: the PDF path with a .md extension).
            pages: 0-based page numbers to convert (default: all pages).
        """
        source = workspace.resolve(directory, path)
        if not source.is_file():
            raise FileNotFoundError(f"{path} is not a file.")
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"{path} is not a PDF file.")

        markdown = convert_pdf(str(source), pages)
        target = output_path or str(source.relative_to(workspace.root(directory)).with_suffix(".md"))
        written = workspace.write(directory, target, markdown)
        return ToolResult.ok({"output": workspace.describe(written), "characters": len(markdown)})

    return [pdf_to_markdown]
