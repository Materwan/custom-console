"""Conversion of reMarkable .rmdoc archives into PDF."""

from __future__ import annotations

import sys
import zipfile

import pytest

from custom_console.fs.rmdoc import (
    PT_PER_PX,
    RM_HEIGHT,
    RM_WIDTH,
    RmdocError,
    default_output,
    read_document,
    read_drawing,
    rmdoc_to_pdf,
)

pymupdf = pytest.importorskip("pymupdf")


def open_pdf(path):
    return pymupdf.open(str(path))


def stroke_paths(page):
    return [d for d in page.get_drawings() if d.get("color") is not None]


class TestDocument:
    def test_old_layout_pages_and_redirections(self, tmp_path, rm):
        path = tmp_path / "a.rmdoc"
        ids = rm.make_rmdoc(path, pdf=rm.make_pdf(3), redirects=[2, -1, 0])
        with zipfile.ZipFile(path) as archive:
            document = read_document(archive)
        assert [(p.page_id, p.source) for p in document.pages] == [(ids[0], 2), (ids[1], None), (ids[2], 0)]
        assert document.file_type == "pdf" and document.name == "Course" and not document.identity

    def test_modern_layout_orders_pages_by_their_index(self, tmp_path, rm):
        path = tmp_path / "a.rmdoc"
        ids = rm.make_rmdoc(path, pdf=rm.make_pdf(2), redirects=[0, 1], modern=True)
        with zipfile.ZipFile(path) as archive:
            document = read_document(archive)
        assert [p.page_id for p in document.pages] == ids and document.identity

    def test_modern_layout_skips_deleted_pages(self, tmp_path, rm):
        import json

        path = tmp_path / "a.rmdoc"
        rm.make_rmdoc(path, pdf=rm.make_pdf(2), redirects=[0, 1], modern=True)
        with zipfile.ZipFile(path) as archive:
            content = json.loads(archive.read(f"{rm.DOC_ID}.content"))
            content["cPages"]["pages"][0]["deleted"] = {"timestamp": "1:2", "value": 1}
            document_before = read_document(archive)
        assert len(document_before.pages) == 2
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{rm.DOC_ID}.content", json.dumps(content))
        with zipfile.ZipFile(path) as archive:
            assert len(read_document(archive).pages) == 1


class TestDrawing:
    def test_strokes_colors_tools_and_highlights(self, rm):
        data = rm.make_page(
            [
                rm.make_line([(0, 0), (10, 10)], "RED"),
                rm.make_line([(0, 0), (10, 10)], "BLUE", tool="HIGHLIGHTER_2", width=100),
                rm.make_line([(0, 0), (10, 10)], tool="ERASER"),  # an eraser leaves no mark
            ],
            highlights=[(-100, 50, 200, 30)],
        )
        drawing = read_drawing(data)
        assert len(drawing.strokes) == 2 and len(drawing.highlights) == 1
        red, blue = drawing.strokes
        assert red.color[0] > 0.8 and red.opacity == 1.0 and red.width == pytest.approx(2.0)
        assert blue.opacity == pytest.approx(0.4) and blue.width == pytest.approx(25.0)
        assert drawing.highlights[0].rectangles == [(-100, 50, 200, 30)]
        assert drawing.bottom == 80  # the highlight is the lowest thing

    def test_a_page_with_nothing_on_it_is_empty(self, rm):
        assert read_drawing(rm.make_page([])).empty


class TestOriginalPdf:
    def test_nothing_to_draw_gives_the_original_bytes(self, tmp_path, rm):
        pdf = rm.make_pdf(3)
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(source, pdf=pdf, drawings={0: rm.make_page([])})
        result = rmdoc_to_pdf(str(source))
        assert (tmp_path / "a.pdf").read_bytes() == pdf
        assert result.output == str(tmp_path / "a.pdf") and not result.handwriting
        assert result.pages == 3 and result.annotated_pages == 0 and result.file_type == "pdf"

    def test_the_original_can_be_asked_for_even_when_there_is_handwriting(self, tmp_path, rm):
        pdf = rm.make_pdf(2)
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(source, pdf=pdf, redirects=[0, 1], drawings={0: rm.make_page([rm.make_line([(0, 0), (5, 5)])])})
        result = rmdoc_to_pdf(str(source), str(tmp_path / "plain.pdf"), handwriting=False)
        assert (tmp_path / "plain.pdf").read_bytes() == pdf and not result.handwriting

    def test_default_output_sits_next_to_the_archive(self):
        assert default_output("/x/y/Course.rmdoc").replace("\\", "/") == "/x/y/Course.pdf"


class TestHandwriting:
    def test_strokes_land_on_the_right_page_and_keep_the_original_content(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        page = rm.make_page([rm.make_line([(-300, 200), (0, 260), (300, 200)], "RED")])
        rm.make_rmdoc(source, pdf=rm.make_pdf(3), drawings={1: page})
        result = rmdoc_to_pdf(str(source), str(tmp_path / "out.pdf"))

        assert result.handwriting and result.annotated_pages == 1 and result.pages == 3
        document = open_pdf(tmp_path / "out.pdf")
        assert document.page_count == 3
        assert [len(stroke_paths(p)) for p in document] == [0, 1, 0]
        assert "Original page 2" in document[1].get_text()  # the PDF's own content is still there

        rect = stroke_paths(document[1])[0]["rect"]
        scale = 595 / RM_WIDTH  # the PDF page is fitted to the tablet's width
        assert rect.width == pytest.approx(600 * scale, rel=0.05)
        assert (rect.x0 + rect.x1) / 2 == pytest.approx(595 / 2, abs=1)  # x = 0 is the middle of the page
        assert rect.y0 == pytest.approx(200 * scale, abs=2)
        red = stroke_paths(document[1])[0]["color"]
        assert red[0] > 0.8 and red[1] < 0.2

    def test_highlights_are_translucent_fills(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(source, pdf=rm.make_pdf(1), redirects=[0], drawings={0: rm.make_page(highlights=[(-200, 100, 300, 40)])})
        rmdoc_to_pdf(str(source), str(tmp_path / "out.pdf"))
        fills = [d for d in open_pdf(tmp_path / "out.pdf")[0].get_drawings() if d.get("fill") is not None]
        assert len(fills) == 1 and fills[0]["fill_opacity"] == pytest.approx(0.4)

    def test_reordered_and_inserted_pages_follow_the_tablets_order(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(
            source,
            pdf=rm.make_pdf(3),
            redirects=[2, -1, 0],
            drawings={1: rm.make_page([rm.make_line([(0, 0), (50, 50)])])},
        )
        result = rmdoc_to_pdf(str(source), str(tmp_path / "out.pdf"))
        document = open_pdf(tmp_path / "out.pdf")
        texts = [p.get_text() for p in document]
        assert "Original page 3" in texts[0] and "Original page" not in texts[1] and "Original page 1" in texts[2]
        assert document[1].rect.width == pytest.approx(RM_WIDTH * PT_PER_PX, abs=0.5)  # an inserted blank page
        assert len(stroke_paths(document[1])) == 1 and result.pages == 3

    def test_modern_layout_converts_like_the_old_one(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(
            source, pdf=rm.make_pdf(2), redirects=[0, 1], modern=True,
            drawings={0: rm.make_page([rm.make_line([(0, 0), (9, 9)])])},
        )
        rmdoc_to_pdf(str(source), str(tmp_path / "out.pdf"))
        document = open_pdf(tmp_path / "out.pdf")
        assert [len(stroke_paths(p)) for p in document] == [1, 0]

    def test_an_unreadable_page_is_reported_and_the_others_are_kept(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(
            source, pdf=rm.make_pdf(2), redirects=[0, 1],
            drawings={0: b"not a scene", 1: rm.make_page([rm.make_line([(0, 0), (9, 9)])])},
        )
        result = rmdoc_to_pdf(str(source), str(tmp_path / "out.pdf"))
        assert any(note.startswith("Page 1:") for note in result.notes)
        assert [len(stroke_paths(p)) for p in open_pdf(tmp_path / "out.pdf")] == [0, 1]


class TestNotebook:
    def test_a_notebook_becomes_blank_pages_with_the_handwriting(self, tmp_path, rm):
        source = tmp_path / "n.rmdoc"
        rm.make_rmdoc(
            source, file_type="notebook", redirects=[-1, -1],
            drawings={0: rm.make_page([rm.make_line([(-100, 100), (100, 400)])])},
        )
        result = rmdoc_to_pdf(str(source))
        document = open_pdf(tmp_path / "n.pdf")
        assert result.pages == 2 and result.file_type == "notebook" and result.handwriting
        assert document[0].rect.width == pytest.approx(RM_WIDTH * PT_PER_PX, abs=0.5)
        assert document[0].rect.height == pytest.approx(RM_HEIGHT * PT_PER_PX, abs=0.5)
        assert [len(stroke_paths(p)) for p in document] == [1, 0]

    def test_a_page_grows_to_hold_what_is_written_below_its_end(self, tmp_path, rm):
        source = tmp_path / "n.rmdoc"
        rm.make_rmdoc(
            source, file_type="notebook", redirects=[-1],
            drawings={0: rm.make_page([rm.make_line([(0, 100), (0, 3000)])])},
        )
        rmdoc_to_pdf(str(source))
        assert open_pdf(tmp_path / "n.pdf")[0].rect.height > 3000 * PT_PER_PX

    def test_the_original_of_a_notebook_does_not_exist(self, tmp_path, rm):
        source = tmp_path / "n.rmdoc"
        rm.make_rmdoc(source, file_type="notebook", redirects=[-1])
        with pytest.raises(RmdocError, match="no original PDF"):
            rmdoc_to_pdf(str(source), handwriting=False)

    def test_an_epub_warns_that_its_text_is_missing(self, tmp_path, rm):
        source = tmp_path / "e.rmdoc"
        rm.make_rmdoc(source, file_type="epub", redirects=[-1], drawings={0: rm.make_page([rm.make_line([(0, 0), (9, 9)])])})
        assert any("EPUB" in note for note in rmdoc_to_pdf(str(source)).notes)


class TestErrors:
    def test_not_an_archive(self, tmp_path):
        (tmp_path / "x.rmdoc").write_text("plain text")
        with pytest.raises(RmdocError, match="not a valid .rmdoc"):
            rmdoc_to_pdf(str(tmp_path / "x.rmdoc"))

    def test_an_archive_that_is_not_a_remarkable_document(self, tmp_path):
        with zipfile.ZipFile(tmp_path / "x.rmdoc", "w") as archive:
            archive.writestr("readme.txt", "hi")
        with pytest.raises(RmdocError, match="not a reMarkable document"):
            rmdoc_to_pdf(str(tmp_path / "x.rmdoc"))

    def test_missing_source(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            rmdoc_to_pdf(str(tmp_path / "nope.rmdoc"))

    def test_an_existing_output_is_kept_unless_overwrite_is_asked(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(source, pdf=rm.make_pdf(1), redirects=[0])
        (tmp_path / "a.pdf").write_bytes(b"precious")
        with pytest.raises(FileExistsError):
            rmdoc_to_pdf(str(source))
        assert (tmp_path / "a.pdf").read_bytes() == b"precious"
        rmdoc_to_pdf(str(source), overwrite=True)
        assert (tmp_path / "a.pdf").read_bytes().startswith(b"%PDF")

    def test_the_output_cannot_be_the_archive_itself(self, tmp_path, rm):
        source = tmp_path / "a.rmdoc"
        rm.make_rmdoc(source, pdf=rm.make_pdf(1), redirects=[0])
        with pytest.raises(RmdocError, match="replace the .rmdoc"):
            rmdoc_to_pdf(str(source), str(source), overwrite=True)
        assert zipfile.is_zipfile(source)

    def test_without_rmscene_the_handwriting_cannot_be_read_but_the_original_can(self, tmp_path, rm, monkeypatch):
        source = tmp_path / "a.rmdoc"
        pdf = rm.make_pdf(1)
        rm.make_rmdoc(source, pdf=pdf, redirects=[0], drawings={0: rm.make_page([])})
        monkeypatch.setitem(sys.modules, "rmscene", None)  # makes `import rmscene` fail
        with pytest.raises(ImportError, match=r"custom-console\[rmdoc\]"):
            rmdoc_to_pdf(str(source))
        rmdoc_to_pdf(str(source), str(tmp_path / "plain.pdf"), handwriting=False)
        assert (tmp_path / "plain.pdf").read_bytes() == pdf
