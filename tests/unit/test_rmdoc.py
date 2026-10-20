from __future__ import annotations

import io
import zipfile

import pytest

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("rmscene")

from custom_console.core.vfs.rmdoc import (  # noqa: E402
    PageRef,
    RmdocError,
    convert,
    page_refs,
    read_document,
    read_drawing,
)
from custom_console.platform import Cancelled, CancelToken  # noqa: E402
from tests.support.rmdoc_builder import line, make_pdf, page, rmdoc  # noqa: E402

STROKE = line([(0, 100), (200, 300), (-300, 500)])


def pdf_of(data: bytes):
    return pymupdf.open(stream=data, filetype="pdf")


def drawn_count(data: bytes, number: int) -> int:
    with pdf_of(data) as document:
        return len(document[number].get_drawings())


def test_an_untouched_pdf_comes_back_byte_for_byte():
    original = make_pdf(3)
    data, _ = rmdoc(pdf=original)
    result = convert(data)
    assert result.pdf == original and not result.handwriting_drawn and result.annotated_pages == 0 and result.pages == 3
    assert result.name == "Course" and result.file_type == "pdf"


def test_handwriting_is_drawn_on_the_original_pages():
    original = make_pdf(3)
    data, _ = rmdoc(pdf=original, drawings={1: page([STROKE, line([(10, 10), (20, 20)], "RED")])})
    result = convert(data)
    assert result.handwriting_drawn and result.annotated_pages == 1 and result.pages == 3
    assert drawn_count(result.pdf, 1) >= 2 and drawn_count(result.pdf, 0) == 0
    with pdf_of(result.pdf) as document:
        assert "Original page 2" in document[1].get_text()


def test_highlights_are_drawn_with_transparency():
    data, _ = rmdoc(pdf=make_pdf(1), page_ids=["p0"], redirects=[0], drawings={0: page(highlights=[(0, 100, 300, 40)])})
    result = convert(data)
    with pdf_of(result.pdf) as document:
        fills = [d for d in document[0].get_drawings() if d.get("fill")]
        assert fills and abs(fills[0]["fill_opacity"] - 0.4) < 0.01


def test_a_notebook_becomes_blank_pages_carrying_the_handwriting():
    data, _ = rmdoc(file_type="notebook", redirects=[-1, -1], drawings={0: page([STROKE]), 1: page([line([(0, 0), (50, 50)])])})
    result = convert(data)
    assert result.file_type == "notebook" and result.pages == 2 and result.annotated_pages == 2
    with pdf_of(result.pdf) as document:
        assert abs(document[0].rect.width - 1404 * 72 / 226) < 0.5
        assert document[0].get_text().strip() == "" and len(document[0].get_drawings()) >= 1


def test_a_blank_page_grows_to_hold_strokes_written_below_the_usual_end():
    low = line([(0, 100), (0, 3000)])
    data, _ = rmdoc(file_type="notebook", redirects=[-1], drawings={0: page([low])})
    with pdf_of(convert(data).pdf) as document:
        assert document[0].rect.height > 1872 * 72 / 226 + 10


def test_pages_follow_the_tablets_order_and_skip_deleted_ones():
    original = make_pdf(4)
    # the tablet shows PDF pages 3, 1 and 0 (page 2 was deleted from the document)
    data, _ = rmdoc(pdf=original, redirects=[3, 1, 2, 0], modern=True, deleted=[2])
    result = convert(data)
    with pdf_of(result.pdf) as document:
        assert [f"Original page {n}" in document[i].get_text() for i, n in enumerate((4, 2, 1))] == [True] * 3
        assert document.page_count == 3


def test_an_inserted_blank_page_stays_blank_and_the_rest_keeps_its_place():
    data, _ = rmdoc(pdf=make_pdf(2), redirects=[0, -1, 1], drawings={1: page([STROKE])})
    with pdf_of(convert(data).pdf) as document:
        assert document.page_count == 3
        assert "Original page 1" in document[0].get_text() and document[1].get_text().strip() == ""
        assert "Original page 2" in document[2].get_text() and len(document[1].get_drawings()) >= 1


def test_the_old_page_layout_with_redirection_map_is_read():
    data, _ = rmdoc(pdf=make_pdf(3), redirects=[2, 0, 1])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = read_document(archive)
    assert [r.source for r in document.pages] == [2, 0, 1] and not document.untouched


def test_page_refs_edge_cases():
    assert page_refs({}, True) == []
    assert page_refs({"pages": ["a", "b"]}, True) == [PageRef("a", 0), PageRef("b", 1)]
    assert page_refs({"pages": ["a", "b"]}, False) == [PageRef("a", None), PageRef("b", None)]
    modern = {"cPages": {"pages": [{"id": "x", "idx": {"value": "b"}, "redir": {"value": 5}},
                                   {"id": "y", "idx": {"value": "a"}}, {"id": "z", "idx": {"value": "c"}, "redir": {"value": -1}}]}}
    assert page_refs(modern, True) == [PageRef("y", 0), PageRef("x", 5), PageRef("z", None)]


def test_erasers_hidden_layers_and_empty_pages_draw_nothing():
    eraser = line([(0, 0), (100, 100)], tool="ERASER")
    for body in (page([eraser]), page([STROKE], hidden_layer=True), page()):
        data, _ = rmdoc(pdf=make_pdf(1), redirects=[0], page_ids=["p0"], drawings={0: body})
        result = convert(data)
        assert not result.handwriting_drawn and result.annotated_pages == 0


def test_read_drawing_reads_strokes_colours_and_widths():
    drawing = read_drawing(page([line([(0, 0), (10, 10), (20, 5)], "BLUE", width=12), line([(1, 1)], "RED")]))
    assert len(drawing.strokes) == 2 and drawing.strokes[0].color[2] > 0.5 and drawing.strokes[0].width >= 1.2
    assert len(drawing.strokes[0].points) == 3 and drawing.bottom == 10.0


def test_without_handwriting_the_original_is_extracted():
    original = make_pdf(2)
    data, _ = rmdoc(pdf=original, redirects=[0, 1], drawings={0: page([STROKE])})
    result = convert(data, handwriting=False)
    assert result.pdf == original and not result.handwriting_drawn
    notebook, _ = rmdoc(file_type="notebook", redirects=[-1])
    with pytest.raises(RmdocError, match="no original PDF"):
        convert(notebook, handwriting=False)


def test_an_unreadable_page_is_reported_and_the_others_are_kept():
    data, _ = rmdoc(pdf=make_pdf(2), redirects=[0, 1], drawings={0: b"garbage", 1: page([STROKE])})
    result = convert(data)
    assert result.annotated_pages == 1 and any("Page 1" in n and "could not be read" in n for n in result.notes)


def test_an_epub_says_that_only_the_handwriting_is_converted():
    data, _ = rmdoc(file_type="epub", redirects=[-1], drawings={0: page([STROKE])})
    assert any("EPUB" in n for n in convert(data).notes)


@pytest.mark.parametrize(("data", "message"), [
    (b"not a zip", "valid .rmdoc"),
    (lambda: (lambda b: (zipfile.ZipFile(b, "w").writestr("x.txt", "hi"), b.getvalue())[1])(io.BytesIO()), "no .content"),
])
def test_bad_archives(data, message):
    with pytest.raises(RmdocError, match=message):
        convert(data() if callable(data) else data)


def test_a_document_with_no_pages_is_refused():
    data, _ = rmdoc(file_type="notebook", page_ids=[], redirects=[])
    with pytest.raises(RmdocError, match="no page"):
        convert(data)


def test_cancel_stops_the_conversion():
    token = CancelToken()
    token.cancel()
    data, _ = rmdoc(pdf=make_pdf(2), redirects=[0, 1], drawings={0: page([STROKE])})
    with pytest.raises(Cancelled):
        convert(data, cancel=token)
