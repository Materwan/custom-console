"""reMarkable ``.rmdoc`` archives to PDF.

An ``.rmdoc`` is a zip archive, which is what ``rmapi get`` downloads. For a
document that was imported as a PDF it holds the original file
(``<id>.pdf``) and, for every page the user wrote on, a ``<id>/<page>.rm`` file
with the pen strokes (reMarkable "lines" format, version 6).

`rmdoc_to_pdf` gives back the original PDF, with the handwriting drawn on top of
it. A document that is not a PDF (a notebook) becomes a PDF of blank pages
carrying the handwriting. The original is copied byte for byte when there is
nothing to draw.

The drawing is an approximation of what the tablet shows: one line of constant
width per stroke (no pressure or speed effects), typed text is not rendered, and
the strokes are placed as the tablet does for a page fitted to the screen width.

Needs ``pymupdf`` and ``rmscene`` (``pip install 'custom-console[rmdoc]'``) as
soon as there is something to draw.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

RM_WIDTH = 1404  # the tablet's page, in pixels; x = 0 is the middle of the page
RM_HEIGHT = 1872
RM_DPI = 226
PT_PER_PX = 72 / RM_DPI  # size of a pixel of the tablet on a blank page, in PDF points
PAGE_MARGIN_PX = 60  # blank pages grow to hold strokes written below the usual page end

MISSING_DEPENDENCY = "Converting a reMarkable document needs: pip install 'custom-console[rmdoc]'"

Color = Tuple[float, float, float]


class RmdocError(Exception):
    """The archive cannot be converted (the message says why)."""


# -- the archive -------------------------------------------------------------------------- #


@dataclass
class PageRef:
    page_id: str
    source: Optional[int] = None  # 0-based page of the original PDF; None = blank page


@dataclass
class Document:
    doc_id: str
    name: str
    file_type: str
    pdf_member: Optional[str]
    pages: List[PageRef] = field(default_factory=list)

    @property
    def identity(self) -> bool:
        """Do the pages show the original PDF untouched (same pages, same order)?"""
        return all(ref.source == index for index, ref in enumerate(self.pages))


def _json(archive: zipfile.ZipFile, member: str) -> Dict[str, Any]:
    try:
        data = json.loads(archive.read(member).decode("utf-8", errors="replace") or "{}")
    except (KeyError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _page_refs(content: Dict[str, Any], is_pdf: bool) -> List[PageRef]:
    """Pages in display order. Two layouts exist: a ``cPages`` list (firmware 3.x)
    and a plain list of ids with a separate ``redirectionPageMap``."""
    modern = content.get("cPages")
    if isinstance(modern, dict) and isinstance(modern.get("pages"), list):
        entries = [p for p in modern["pages"] if isinstance(p, dict) and p.get("id") and "deleted" not in p]
        if all(isinstance(p.get("idx"), dict) and "value" in p["idx"] for p in entries):
            entries.sort(key=lambda p: str(p["idx"]["value"]))  # the tablet orders pages by this key
        refs = []
        for index, entry in enumerate(entries):
            redirect = entry.get("redir", {}).get("value") if isinstance(entry.get("redir"), dict) else None
            if redirect is None and is_pdf:
                redirect = index
            refs.append(PageRef(str(entry["id"]), redirect if isinstance(redirect, int) and redirect >= 0 else None))
        return refs

    ids = content.get("pages")
    if not isinstance(ids, list):
        return []
    redirects = content.get("redirectionPageMap")
    refs = []
    for index, page_id in enumerate(ids):
        redirect: Any = index if is_pdf else None
        if isinstance(redirects, list) and index < len(redirects):
            redirect = redirects[index]
        refs.append(PageRef(str(page_id), redirect if isinstance(redirect, int) and redirect >= 0 else None))
    return refs


def read_document(archive: zipfile.ZipFile) -> Document:
    names = archive.namelist()
    contents = [n for n in names if n.endswith(".content") and "/" not in n]
    if not contents:
        raise RmdocError("this is not a reMarkable document (no .content file in the archive).")
    doc_id = contents[0][: -len(".content")]
    content = _json(archive, contents[0])
    metadata = _json(archive, f"{doc_id}.metadata")
    pdf_member = f"{doc_id}.pdf" if f"{doc_id}.pdf" in names else None
    file_type = str(content.get("fileType") or ("pdf" if pdf_member else "notebook")).lower()
    return Document(
        doc_id=doc_id,
        name=str(metadata.get("visibleName") or doc_id),
        file_type=file_type,
        pdf_member=pdf_member,
        pages=_page_refs(content, is_pdf=pdf_member is not None),
    )


# -- the handwriting ---------------------------------------------------------------------- #


@dataclass
class Stroke:
    points: List[Tuple[float, float]]  # tablet pixels
    color: Color
    width: float  # pixels
    opacity: float = 1.0


@dataclass
class Highlight:
    rectangles: List[Tuple[float, float, float, float]]  # x, y, width, height in tablet pixels
    color: Color


@dataclass
class Drawing:
    strokes: List[Stroke] = field(default_factory=list)
    highlights: List[Highlight] = field(default_factory=list)
    has_text: bool = False

    @property
    def empty(self) -> bool:
        return not self.strokes and not self.highlights

    @property
    def bottom(self) -> float:
        ys = [y for stroke in self.strokes for _, y in stroke.points]
        ys += [y + h for highlight in self.highlights for _, y, _, h in highlight.rectangles]
        return max(ys, default=0.0)


PALETTE: Dict[str, Color] = {
    "BLACK": (0.0, 0.0, 0.0),
    "GRAY": (0.5, 0.5, 0.5),
    "GRAY_OVERLAP": (0.5, 0.5, 0.5),
    "WHITE": (1.0, 1.0, 1.0),
    "YELLOW": (1.0, 0.92, 0.0),
    "YELLOW_2": (1.0, 0.92, 0.0),
    "HIGHLIGHT": (1.0, 0.92, 0.0),
    "GREEN": (0.1, 0.7, 0.25),
    "GREEN_2": (0.25, 0.8, 0.35),
    "PINK": (1.0, 0.3, 0.6),
    "BLUE": (0.1, 0.25, 0.85),
    "RED": (0.85, 0.05, 0.05),
    "CYAN": (0.0, 0.75, 0.85),
    "MAGENTA": (0.85, 0.1, 0.75),
}

ERASERS = ("ERASER", "ERASER_AREA")
OPACITY_BY_TOOL = {"HIGHLIGHTER_1": 0.4, "HIGHLIGHTER_2": 0.4, "SHADER": 0.25, "PENCIL_1": 0.85, "PENCIL_2": 0.85}
MIN_WIDTH_PX = 1.2
HIGHLIGHT_OPACITY = 0.4


def _color(item: Any) -> Color:
    rgba = getattr(item, "color_rgba", None)
    if rgba:
        return (rgba[0] / 255, rgba[1] / 255, rgba[2] / 255)
    name = getattr(getattr(item, "color", None), "name", "BLACK")
    return PALETTE.get(name, PALETTE["BLACK"])


def _stroke_of(line: Any) -> Optional[Stroke]:
    tool = getattr(getattr(line, "tool", None), "name", "")
    points = list(getattr(line, "points", None) or [])
    if tool in ERASERS or not points:
        return None
    widths = [p.width for p in points if getattr(p, "width", 0)]
    width = max(MIN_WIDTH_PX, (sum(widths) / len(widths) / 4) if widths else MIN_WIDTH_PX)
    return Stroke(
        [(p.x, p.y) for p in points],
        _color(line),
        width,
        OPACITY_BY_TOOL.get(tool, 1.0),
    )


def _collect(group: Any, drawing: Drawing) -> None:
    """Strokes and highlights of a scene group, layers included (deleted items
    and hidden layers are skipped)."""
    if not getattr(group, "visible", None) or group.visible.value:
        for item in group.children.values():
            if item is None:
                continue  # deleted
            kind = type(item).__name__
            if kind == "Group":
                _collect(item, drawing)
            elif kind == "Line":
                stroke = _stroke_of(item)
                if stroke is not None:
                    drawing.strokes.append(stroke)
            elif kind == "GlyphRange":
                rectangles = [(r.x, r.y, r.w, r.h) for r in item.rectangles]
                if rectangles:
                    drawing.highlights.append(Highlight(rectangles, _color(item)))


def read_drawing(data: bytes) -> Drawing:
    """The handwriting of one ``.rm`` page."""
    try:
        from rmscene import read_tree
    except ImportError as error:
        raise ImportError(MISSING_DEPENDENCY) from error
    tree = read_tree(io.BytesIO(data))
    drawing = Drawing()
    _collect(tree.root, drawing)
    text = getattr(tree, "root_text", None)
    drawing.has_text = bool(text is not None and getattr(text, "items", None))
    return drawing


# -- the PDF ------------------------------------------------------------------------------ #


@dataclass
class Conversion:
    output: str
    pages: int
    annotated_pages: int
    file_type: str
    handwriting: bool  # was the handwriting drawn (False: the original was copied as it is)
    notes: List[str] = field(default_factory=list)


def _draw(page: Any, drawing: Drawing, scale: float) -> None:
    import pymupdf

    origin_x = page.rect.width / 2
    back = page.derotation_matrix if page.rotation else pymupdf.Matrix(1, 1)

    def at(x: float, y: float) -> Any:
        return pymupdf.Point(origin_x + x * scale, y * scale) * back

    for highlight in drawing.highlights:
        for x, y, w, h in highlight.rectangles:
            rect = pymupdf.Rect(at(x, y), at(x + w, y + h)).normalize()
            page.draw_rect(rect, color=None, fill=highlight.color, fill_opacity=HIGHLIGHT_OPACITY, width=0)

    shape = page.new_shape()
    for stroke in drawing.strokes:
        points = [at(x, y) for x, y in stroke.points]
        if len(points) == 1:  # a dot
            points.append(pymupdf.Point(points[0].x + 0.01, points[0].y))
        shape.draw_polyline(points)
        shape.finish(
            color=stroke.color,
            fill=None,
            width=stroke.width * scale,
            closePath=False,
            lineCap=1,
            lineJoin=1,
            stroke_opacity=stroke.opacity,
        )
    shape.commit()


def _assemble(pdf: Optional[bytes], document: Document, drawings: Dict[str, Drawing]) -> bytes:
    import pymupdf

    source = pymupdf.open(stream=pdf, filetype="pdf") if pdf else None
    out = pymupdf.open()
    try:
        runs: List[Tuple[Optional[int], int]] = []  # (first source page or None, how many pages)
        for ref in document.pages:
            usable = source is not None and ref.source is not None and ref.source < source.page_count
            if usable and runs and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == ref.source:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((ref.source if usable else None, 1))

        index = 0
        for first, count in runs:
            if first is not None:
                out.insert_pdf(source, from_page=first, to_page=first + count - 1)
                for offset in range(count):
                    page = out[index + offset]
                    _draw_page(page, drawings.get(document.pages[index + offset].page_id), page.rect.width / RM_WIDTH)
            else:
                drawing = drawings.get(document.pages[index].page_id)
                height = RM_HEIGHT
                if drawing is not None:
                    height = max(RM_HEIGHT, drawing.bottom + PAGE_MARGIN_PX)
                page = out.new_page(width=RM_WIDTH * PT_PER_PX, height=height * PT_PER_PX)
                _draw_page(page, drawing, PT_PER_PX)
            index += count
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()
        if source is not None:
            source.close()


def _draw_page(page: Any, drawing: Optional[Drawing], scale: float) -> None:
    if drawing is not None and not drawing.empty:
        _draw(page, drawing, scale)


def _write(target: str, data: bytes, overwrite: bool) -> None:
    if os.path.isdir(target):
        raise RmdocError(f"{target} is a folder.")
    if os.path.exists(target) and not overwrite:
        raise FileExistsError(f"{target} already exists (use -f to replace it).")
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def default_output(source: str) -> str:
    return os.path.splitext(source)[0] + ".pdf"


def rmdoc_to_pdf(
    source: str,
    target: Optional[str] = None,
    *,
    handwriting: bool = True,
    overwrite: bool = False,
) -> Conversion:
    """Convert the ``.rmdoc`` archive `source` into the PDF `target` (default: next
    to it, with a .pdf extension).

    With `handwriting` the pen strokes and highlights are drawn on the pages; without
    it the original PDF is extracted as it is (which needs an original PDF).
    """
    target = target or default_output(source)
    if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(source)):
        raise RmdocError("the output file would replace the .rmdoc itself.")
    if not os.path.isfile(source):
        raise FileNotFoundError(f"{source} is not a file.")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as error:
        raise RmdocError(f"{os.path.basename(source)} is not a valid .rmdoc archive (a zip file).") from error

    with archive:
        document = read_document(archive)
        pdf = archive.read(document.pdf_member) if document.pdf_member else None
        if pdf is None and not handwriting:
            raise RmdocError(
                f"this document has no original PDF (it is a {document.file_type}): "
                "its handwriting is all there is to convert."
            )
        if not document.pages and pdf is None:
            raise RmdocError("the document has no page.")

        notes: List[str] = []
        if document.file_type == "epub":
            notes.append("This is an EPUB: only the handwriting is converted, the book's text is not included.")

        drawings: Dict[str, Drawing] = {}
        if handwriting:
            members = set(archive.namelist())
            for number, ref in enumerate(document.pages, start=1):
                member = f"{document.doc_id}/{ref.page_id}.rm"
                if member not in members:
                    continue
                try:
                    drawing = read_drawing(archive.read(member))
                except ImportError:
                    raise
                except Exception as error:  # one unreadable page must not lose the others
                    notes.append(f"Page {number}: the handwriting could not be read ({error}).")
                    continue
                if drawing.has_text:
                    notes.append(f"Page {number}: typed text is not rendered.")
                if not drawing.empty:
                    drawings[ref.page_id] = drawing

        drawn = len(drawings)
        if pdf is not None and drawn == 0 and (document.identity or not document.pages):
            data, handwriting_drawn = pdf, False  # nothing to add: the original, untouched
        elif pdf is not None and not handwriting:
            data, handwriting_drawn = pdf, False
        else:
            try:
                data = _assemble(pdf, document, drawings)
            except ImportError as error:
                raise ImportError(MISSING_DEPENDENCY) from error
            handwriting_drawn = drawn > 0

    _write(target, data, overwrite)
    pages = _count_pages(data) or len(document.pages)
    return Conversion(target, pages, drawn, document.file_type, handwriting_drawn, notes)


def _count_pages(data: bytes) -> int:
    """Pages of a PDF; 0 when they cannot be counted (pymupdf missing): only the report suffers."""
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as document:
            return document.page_count
    except Exception:
        return 0
