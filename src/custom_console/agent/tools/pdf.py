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
        """PDF of a workspace -> Markdown file (formulas, tables kept; bring the PDF with
        `workspace_file_get`, then read the .md). output_path default: same name in .md;
        pages: 0-based, default all."""
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
