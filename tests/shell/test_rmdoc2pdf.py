"""The rmdoc2pdf shell command."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from custom_console.fs import FileManager
from custom_console.settings import load_settings
from custom_console.shell import Shell
from custom_console.shell.printer import Printer


@pytest.fixture
def run(tmp_path):
    buffer = io.StringIO()
    settings = load_settings({}, root=tmp_path, use_dotenv=False)
    shell = Shell(
        settings,
        printer=Printer(Console(file=buffer, force_terminal=False, width=400)),
        files=FileManager(start_dir=str(tmp_path)),
        confirm=lambda question: True,
    )

    def execute(line: str) -> str:
        buffer.seek(0)
        buffer.truncate()
        shell.execute(line)
        return buffer.getvalue()

    return execute


def test_converts_and_reports_the_result(run, tmp_path, rm):
    pdf = rm.make_pdf(2)
    rm.make_rmdoc(tmp_path / "Course.rmdoc", pdf=pdf, redirects=[0, 1])
    out = run("rmdoc2pdf Course.rmdoc")
    assert "Course.pdf" in out and "2 page(s)" in out
    assert (tmp_path / "Course.pdf").read_bytes() == pdf


def test_mentions_the_handwriting_it_drew_and_what_it_could_not_read(run, tmp_path, rm):
    page = rm.make_page([rm.make_line([(0, 0), (9, 9)])])
    rm.make_rmdoc(tmp_path / "c.rmdoc", pdf=rm.make_pdf(2), redirects=[0, 1], drawings={1: page, 0: b"junk"})
    out = run("rmdoc2pdf c.rmdoc out.pdf")
    assert "handwriting on 1" in out and "Page 1:" in out


def test_an_existing_pdf_needs_force_and_original_skips_the_handwriting(run, tmp_path, rm):
    pdf = rm.make_pdf(1)
    page = rm.make_page([rm.make_line([(0, 0), (9, 9)])])
    rm.make_rmdoc(tmp_path / "c.rmdoc", pdf=pdf, redirects=[0], drawings={0: page})
    run("rmdoc2pdf c.rmdoc")
    assert (tmp_path / "c.pdf").read_bytes() != pdf  # the strokes were drawn
    assert "already exists" in run("rmdoc2pdf c.rmdoc")
    run("rmdoc2pdf -f --original c.rmdoc")
    assert (tmp_path / "c.pdf").read_bytes() == pdf


def test_mistakes_are_shown_as_errors(run, tmp_path):
    (tmp_path / "bad.rmdoc").write_text("nope")
    assert "not a valid .rmdoc" in run("rmdoc2pdf bad.rmdoc")
    assert "missing.rmdoc" in run("rmdoc2pdf missing.rmdoc")
    assert "usage: rmdoc2pdf" in run("rmdoc2pdf -h")
