"""reMarkable `.rmdoc` archives to PDF.

An `.rmdoc` is the zip archive `rmapi get` downloads. For a document imported as a PDF it holds the original (`<id>.pdf`) and,
for each page written on, a `<id>/<page>.rm` file with the pen strokes (the tablet's "lines" format, version 6). A notebook has
only the `.rm` pages.

`convert` gives back the original PDF with the handwriting drawn on top; a notebook becomes a PDF of blank pages carrying
the handwriting. With nothing to draw, the original is returned byte for byte.

What is drawn is an approximation of what the tablet shows: one line of constant width per stroke (no pressure or speed
effects), highlights at 40% opacity, strokes placed as the tablet does for a page fitted to the screen width. Typed text is not
rendered (a note says on which page). Needs `pymupdf` and `rmscene` (the `rmdoc` extra) as soon as there is something to draw.

This module is its own: nothing in it knows about `rmapi`, so the backend and the converter can change independently.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

from ...platform import CancelToken

RM_WIDTH = 1404  # the tablet's page, in pixels; x = 0 is the middle of the page
RM_HEIGHT = 1872
RM_DPI = 226
PT_PER_PX = 72 / RM_DPI  # a tablet pixel on a blank page, in PDF points
PAGE_MARGIN_PX = 60  # blank pages grow to hold strokes written below the usual page end
MIN_WIDTH_PX = 1.2
HIGHLIGHT_OPACITY = 0.4
MAX_ARCHIVE_BYTES = 500_000_000  # refuse absurd archives (a zip bomb) before reading them

MISSING = "Converting a reMarkable document needs the rmdoc extra: uv tool install 'custom-console[rmdoc]'."

Color = tuple[float, float, float]

PALETTE: dict[str, Color] = {
    "BLACK": (0.0, 0.0, 0.0), "GRAY": (0.5, 0.5, 0.5), "GRAY_OVERLAP": (0.5, 0.5, 0.5), "WHITE": (1.0, 1.0, 1.0),
    "YELLOW": (1.0, 0.92, 0.0), "YELLOW_2": (1.0, 0.92, 0.0), "HIGHLIGHT": (1.0, 0.92, 0.0), "GREEN": (0.1, 0.7, 0.25),
    "GREEN_2": (0.25, 0.8, 0.35), "PINK": (1.0, 0.3, 0.6), "BLUE": (0.1, 0.25, 0.85), "RED": (0.85, 0.05, 0.05),
    "CYAN": (0.0, 0.75, 0.85), "MAGENTA": (0.85, 0.1, 0.75),
}
ERASERS = frozenset({"ERASER", "ERASER_AREA"})
OPACITY = {"HIGHLIGHTER_1": 0.4, "HIGHLIGHTER_2": 0.4, "SHADER": 0.25, "PENCIL_1": 0.85, "PENCIL_2": 0.85}


class RmdocError(Exception):
    """The archive cannot be converted; the message says why."""


class MissingDependency(RmdocError):
    pass


# ---------------------------------------------------------------------- the archive
@dataclass(frozen=True, slots=True)
class PageRef:
    page_id: str
    source: int | None = None  # 0-based page of the original PDF; None: a blank page


@dataclass(slots=True)
class Document:
    doc_id: str
    name: str
    file_type: str
    pdf_member: str | None
    pages: list[PageRef] = field(default_factory=list)

    @property
    def untouched(self) -> bool:
        """Do the pages show the original PDF as it is (same pages, same order)?"""
        return all(ref.source == index for index, ref in enumerate(self.pages))


def _json(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    try:
        data = json.loads(archive.read(member).decode("utf-8", errors="replace") or "{}")
    except (KeyError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _redirect(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def page_refs(content: dict[str, Any], is_pdf: bool) -> list[PageRef]:
    """Pages in display order. Two layouts exist: `cPages` (firmware 3.x) and a plain list of ids with `redirectionPageMap`."""
    modern = content.get("cPages")
    if isinstance(modern, dict) and isinstance(modern.get("pages"), list):
        entries = [p for p in modern["pages"] if isinstance(p, dict) and p.get("id") and "deleted" not in p]
        if all(isinstance(p.get("idx"), dict) and "value" in p["idx"] for p in entries):
            entries.sort(key=lambda p: str(p["idx"]["value"]))  # the tablet orders pages by this key
        refs: list[PageRef] = []
        for index, entry in enumerate(entries):
            redir = entry.get("redir")
            value = redir.get("value") if isinstance(redir, dict) else None
            if value is None and is_pdf:
                value = index  # no redirection recorded: the page shows the PDF page at the same position
            refs.append(PageRef(str(entry["id"]), _redirect(value)))
        return refs
    ids = content.get("pages")
    if not isinstance(ids, list):
        return []
    redirects = content.get("redirectionPageMap")
    out: list[PageRef] = []
    for index, page_id in enumerate(ids):
        source: Any = index if is_pdf else None
        if isinstance(redirects, list) and index < len(redirects):
            source = redirects[index]
        out.append(PageRef(str(page_id), _redirect(source)))
    return out


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
    return Document(doc_id, str(metadata.get("visibleName") or doc_id), file_type, pdf_member,
                    page_refs(content, is_pdf=pdf_member is not None))


# ---------------------------------------------------------------------- the handwriting
@dataclass(frozen=True, slots=True)
class Stroke:
    points: tuple[tuple[float, float], ...]  # tablet pixels
    color: Color
    width: float  # pixels
    opacity: float = 1.0


@dataclass(frozen=True, slots=True)
class Highlight:
    rectangles: tuple[tuple[float, float, float, float], ...]  # x, y, width, height in tablet pixels
    color: Color


@dataclass(slots=True)
class Drawing:
    strokes: list[Stroke] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)
    has_text: bool = False

    @property
    def empty(self) -> bool:
        return not self.strokes and not self.highlights

    @property
    def bottom(self) -> float:
        ys = [y for stroke in self.strokes for _, y in stroke.points]
        ys += [y + h for light in self.highlights for _, y, _, h in light.rectangles]
        return max(ys, default=0.0)


def _color(item: Any) -> Color:
    rgba = getattr(item, "color_rgba", None)
    if rgba:
        return (rgba[0] / 255, rgba[1] / 255, rgba[2] / 255)
    return PALETTE.get(getattr(getattr(item, "color", None), "name", "BLACK"), PALETTE["BLACK"])


def _stroke(line: Any) -> Stroke | None:
    tool = getattr(getattr(line, "tool", None), "name", "")
    points = list(getattr(line, "points", None) or [])
    if tool in ERASERS or not points:
        return None
    widths = [p.width for p in points if getattr(p, "width", 0)]
    width = max(MIN_WIDTH_PX, (sum(widths) / len(widths) / 4) if widths else MIN_WIDTH_PX)
    return Stroke(tuple((p.x, p.y) for p in points), _color(line), width, OPACITY.get(tool, 1.0))


def _collect(group: Any, drawing: Drawing) -> None:
    """Strokes and highlights of a scene group, layers included; deleted items and hidden layers are skipped."""
    visible = getattr(group, "visible", None)
    if visible is not None and not visible.value:
        return
    for item in group.children.values():
        if item is None:
            continue  # deleted
        kind = type(item).__name__
        if kind == "Group":
            _collect(item, drawing)
        elif kind == "Line":
            stroke = _stroke(item)
            if stroke is not None:
                drawing.strokes.append(stroke)
        elif kind == "GlyphRange":
            rectangles = tuple((r.x, r.y, r.w, r.h) for r in item.rectangles)
            if rectangles:
                drawing.highlights.append(Highlight(rectangles, _color(item)))


def read_drawing(data: bytes) -> Drawing:
    """The handwriting of one `.rm` page."""
    try:
        from rmscene import read_tree
    except ImportError:
        raise MissingDependency(MISSING) from None
    tree = read_tree(io.BytesIO(data))
    drawing = Drawing()
    _collect(tree.root, drawing)
    text = getattr(tree, "root_text", None)
    drawing.has_text = bool(text is not None and getattr(text, "items", None))
    return drawing


# ---------------------------------------------------------------------- the PDF
@dataclass(slots=True)
class Converted:
    pdf: bytes
    pages: int
    annotated_pages: int
    file_type: str
    name: str
    handwriting_drawn: bool  # False: the original was returned as it is
    notes: list[str] = field(default_factory=list)


def _draw(page: Any, drawing: Drawing, scale: float) -> None:
    import pymupdf

    origin_x = page.rect.width / 2
    back = page.derotation_matrix if page.rotation else pymupdf.Matrix(1, 1)

    def at(x: float, y: float) -> Any:
        return pymupdf.Point(origin_x + x * scale, y * scale) * back

    for light in drawing.highlights:
        for x, y, w, h in light.rectangles:
            page.draw_rect(pymupdf.Rect(at(x, y), at(x + w, y + h)).normalize(), color=None, fill=light.color,
                           fill_opacity=HIGHLIGHT_OPACITY, width=0)
    shape = page.new_shape()
    for stroke in drawing.strokes:
        points = [at(x, y) for x, y in stroke.points]
        if len(points) == 1:  # a dot
            points.append(pymupdf.Point(points[0].x + 0.01, points[0].y))
        shape.draw_polyline(points)
        shape.finish(color=stroke.color, fill=None, width=stroke.width * scale, closePath=False, lineCap=1, lineJoin=1,
                     stroke_opacity=stroke.opacity)
    shape.commit()


def _assemble(pdf: bytes | None, document: Document, drawings: dict[str, Drawing], cancel: CancelToken | None) -> bytes:
    import pymupdf

    source = pymupdf.open(stream=pdf, filetype="pdf") if pdf else None
    out = pymupdf.open()
    try:
        runs: list[tuple[int | None, int]] = []  # (first source page or None for blank pages, how many pages)
        for ref in document.pages:
            usable = source is not None and ref.source is not None and ref.source < source.page_count
            if usable and runs and runs[-1][0] is not None and runs[-1][0] + runs[-1][1] == ref.source:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((ref.source if usable else None, 1))
        index = 0
        for first, count in runs:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if first is not None:
                out.insert_pdf(source, from_page=first, to_page=first + count - 1)
                for offset in range(count):
                    page = out[index + offset]
                    found = drawings.get(document.pages[index + offset].page_id)
                    if found is not None and not found.empty:
                        _draw(page, found, page.rect.width / RM_WIDTH)
            else:
                found = drawings.get(document.pages[index].page_id)
                height = max(RM_HEIGHT, found.bottom + PAGE_MARGIN_PX) if found is not None else RM_HEIGHT
                page = out.new_page(width=RM_WIDTH * PT_PER_PX, height=height * PT_PER_PX)
                if found is not None and not found.empty:
                    _draw(page, found, PT_PER_PX)
            index += count
        return bytes(out.tobytes(garbage=3, deflate=True))
    finally:
        out.close()
        if source is not None:
            source.close()


def count_pages(pdf: bytes) -> int:
    """Pages of a PDF; 0 when they cannot be counted (only the report suffers)."""
    try:
        import pymupdf
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            return int(document.page_count)
    except Exception:
        return 0


def convert(data: bytes, *, handwriting: bool = True, cancel: CancelToken | None = None) -> Converted:
    """Convert the bytes of an `.rmdoc` archive into the bytes of a PDF.

    With `handwriting=False` the original PDF is extracted untouched (which needs an original PDF).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RmdocError("this is not a valid .rmdoc archive (a zip file).") from None
    with archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_ARCHIVE_BYTES:
            raise RmdocError("the archive is far too large to be a reMarkable document.")
        document = read_document(archive)
        pdf = archive.read(document.pdf_member) if document.pdf_member else None
        if pdf is None and not handwriting:
            raise RmdocError(f"this document has no original PDF (it is a {document.file_type}): its handwriting is all there "
                             "is to convert.")
        if not document.pages and pdf is None:
            raise RmdocError("the document has no page.")
        notes: list[str] = []
        if document.file_type == "epub":
            notes.append("This is an EPUB: only the handwriting is converted, the book's text is not included.")
        drawings: dict[str, Drawing] = {}
        if handwriting:
            members = set(archive.namelist())
            for number, ref in enumerate(document.pages, start=1):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                member = f"{document.doc_id}/{ref.page_id}.rm"
                if member not in members:
                    continue
                try:
                    drawing = read_drawing(archive.read(member))
                except MissingDependency:
                    raise
                except Exception as error:  # one unreadable page must not lose the others
                    notes.append(f"Page {number}: the handwriting could not be read ({type(error).__name__}).")
                    continue
                if drawing.has_text:
                    notes.append(f"Page {number}: typed text is not rendered.")
                if not drawing.empty:
                    drawings[ref.page_id] = drawing
        drawn = len(drawings)
        if pdf is not None and ((drawn == 0 and (document.untouched or not document.pages)) or not handwriting):
            output, wrote = pdf, False  # nothing to add: the original, byte for byte
        else:
            try:
                output = _assemble(pdf, document, drawings, cancel)
            except ImportError:
                raise MissingDependency(MISSING) from None
            wrote = drawn > 0
    return Converted(output, count_pages(output) or len(document.pages), drawn, document.file_type, document.name, wrote, notes)
