"""The rmdoc_to_pdf agent tool."""

from __future__ import annotations

from pathlib import Path

from custom_console.agent.tools.pdf import pdf_tools


def tool_of(ctx):
    return {t.__name__: t for t in pdf_tools(ctx)}["rmdoc_to_pdf"]


class TestRmdocTool:
    def test_converts_next_to_the_archive_without_asking_in_the_free_zone(self, make_ctx, files_dir, rm):
        ctx, log = make_ctx(zone=True, auto_level=0)
        pdf = rm.make_pdf(2)
        rm.make_rmdoc(files_dir / "Course.rmdoc", pdf=pdf, redirects=[0, 1])
        result = tool_of(ctx)("Course.rmdoc")
        assert result.success and not log.asked
        assert (files_dir / "Course.pdf").read_bytes() == pdf
        assert result.data == {
            "output": result.data["output"],
            "type": "pdf",
            "pages": 2,
            "pages_with_handwriting": 0,
            "notes": [],
        }
        assert Path(result.data["output"]) == files_dir / "Course.pdf"

    def test_handwriting_can_be_left_out_and_the_output_chosen(self, make_ctx, files_dir, rm):
        ctx, _ = make_ctx(zone=True, auto_level=0)
        pdf = rm.make_pdf(1)
        page = rm.make_page([rm.make_line([(0, 0), (9, 9)])])
        rm.make_rmdoc(files_dir / "c.rmdoc", pdf=pdf, redirects=[0], drawings={0: page})
        tool = tool_of(ctx)
        drawn = tool("c.rmdoc", output_path="out/drawn.pdf")
        plain = tool("c.rmdoc", output_path="out/plain.pdf", include_handwriting=False)
        assert drawn.data["pages_with_handwriting"] == 1 and (files_dir / "out" / "drawn.pdf").read_bytes() != pdf
        assert plain.data["pages_with_handwriting"] == 0 and (files_dir / "out" / "plain.pdf").read_bytes() == pdf

    def test_outside_the_free_zone_it_asks(self, make_ctx, tmp_path, rm):
        ctx, log = make_ctx(zone=True, auto_level=1, answer=False)
        rm.make_rmdoc(tmp_path / "far.rmdoc", pdf=rm.make_pdf(1), redirects=[0])
        result = tool_of(ctx)(str(tmp_path / "far.rmdoc"))
        assert not result.success and len(log.asked) == 1 and not (tmp_path / "far.pdf").exists()

    def test_an_existing_pdf_is_replaced_but_can_be_undone(self, make_ctx, files_dir, rm):
        ctx, _ = make_ctx(zone=True, auto_level=0)
        rm.make_rmdoc(files_dir / "c.rmdoc", pdf=rm.make_pdf(1), redirects=[0])
        (files_dir / "c.pdf").write_bytes(b"old version")
        ctx.checkpoints.begin_turn("convert")
        assert tool_of(ctx)("c.rmdoc").success
        assert (files_dir / "c.pdf").read_bytes().startswith(b"%PDF")
        ctx.checkpoints.undo()
        assert (files_dir / "c.pdf").read_bytes() == b"old version"

    def test_failures_are_reported_not_raised(self, ctx, files_dir):
        tool = tool_of(ctx)
        (files_dir / "bad.rmdoc").write_text("not a zip")
        assert "not a valid .rmdoc" in str(tool("bad.rmdoc").error)
        assert isinstance(tool("missing.rmdoc").error, FileNotFoundError)

    def test_the_remarkable_itself_is_not_a_source(self, ctx):
        assert not tool_of(ctx)("reMarkable:/Course").success
