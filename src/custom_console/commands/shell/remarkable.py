"""reMarkable: `rmdoc2pdf` converts a `.rmdoc` file (or a document on the tablet) into a PDF."""

from __future__ import annotations

import argparse
import posixpath
from collections.abc import Iterator

from ...core.vfs.backend import VfsError
from ...core.vfs.rmdoc import RmdocError, convert
from ..base import CommandSpec as Spec
from ..base import Context, Line
from .helpers import DIM, plural


def _build(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="a .rmdoc file, or a document on the tablet (reMarkable:Notes/Plan)")
    p.add_argument("output", nargs="?", help="the PDF to write (default: next to the source, with a .pdf extension)")
    p.add_argument("-o", "--original", action="store_true", help="take the original PDF without the handwriting")
    p.add_argument("-f", "--force", action="store_true", help="replace the output if it exists")


def _run(ctx: Context, a: argparse.Namespace) -> Iterator[Line]:
    vfs = ctx.vfs
    try:
        source = vfs.resolve(a.source)
        info = vfs.stat(source)
        if info is None:
            ctx.fail(f"{a.source}: No such file")
            return
        if info.is_dir:
            ctx.fail(f"{a.source}: Is a folder")
            return
        if a.output:
            output = vfs.resolve(a.output)
            if vfs.is_dir(output):
                output = vfs.child(output, posixpath.splitext(posixpath.basename(source.virtual))[0] + ".pdf")
        else:
            name = posixpath.splitext(posixpath.basename(source.virtual.rstrip("/")))[0] + ".pdf"
            folder = vfs.here() if source.mount == "tablet" else vfs.resolver.parent(source)
            output = vfs.child(folder, name)
        if output.virtual == source.virtual:
            ctx.fail("the output would replace the .rmdoc itself")
            return
        if vfs.exists(output) and not a.force:
            ctx.fail(f"{output.virtual} already exists (use -f to replace it)")
            return
        ctx.shell.spinner("reading the document")
        try:
            data = vfs.read_bytes(source)
        finally:
            ctx.shell.spinner(None)
        ctx.shell.spinner("converting")
        try:
            result = convert(data, handwriting=not a.original, cancel=ctx.shell.cancel)
        finally:
            ctx.shell.spinner(None)
        vfs.write_bytes(output, result.pdf)
    except RmdocError as error:
        ctx.fail(str(error))
        return
    except VfsError as error:
        ctx.fail(f"{error.path or a.source}: {error.message}")
        return
    kind = "with the handwriting drawn on it" if result.handwriting_drawn else "the original, unchanged"
    yield Line.of(f"Wrote {output.virtual}: {plural(result.pages, 'page')}, {result.annotated_pages} annotated ({kind}).")
    for note in result.notes:
        yield Line.of(note, DIM)


COMMANDS = (
    Spec("rmdoc2pdf", "Convert a reMarkable .rmdoc document into a PDF, handwriting included.", _build, _run,
         kinds=("file", "any"), group="reMarkable"),
)
