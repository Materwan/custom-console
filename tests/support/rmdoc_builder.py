"""Small but real reMarkable documents for the tests, built with pymupdf and rmscene."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Sequence
from typing import Any

DOC_ID = "aef1c475-7e00-4e6b-b399-9c65b51588ad"


def make_pdf(pages: int = 3, width: float = 595, height: float = 842) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for number in range(pages):
        page = document.new_page(width=width, height=height)
        page.insert_text((72, 100), f"Original page {number + 1}")
    data = bytes(document.tobytes())
    document.close()
    return data


def line(points: Sequence[tuple[float, float]], color: str = "BLACK", tool: str = "FINELINER_2", width: int = 8) -> Any:
    from rmscene import scene_items as si

    return si.Line(color=si.PenColor[color], tool=si.Pen[tool],
                   points=[si.Point(x, y, 10, 0, width, 100) for x, y in points], thickness_scale=1.0, starting_length=0.0)


def page(lines: Sequence[Any] = (), highlights: Sequence[tuple[float, float, float, float]] = (), *, hidden_layer: bool = False) -> bytes:
    """The bytes of one `.rm` page: a layer holding `lines`, and text highlights given as x, y, w, h."""
    from rmscene import (
        AuthorIdsBlock,
        CrdtId,
        CrdtSequenceItem,
        MigrationInfoBlock,
        PageInfoBlock,
        SceneGlyphItemBlock,
        SceneGroupItemBlock,
        SceneLineItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
        write_blocks,
    )
    from rmscene import scene_items as si

    root, layer = CrdtId(0, 1), CrdtId(0, 11)
    blocks: list[Any] = [
        AuthorIdsBlock(author_uuids={1: uuid.uuid4()}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=root),
        TreeNodeBlock(si.Group(node_id=root)),
        TreeNodeBlock(si.Group(node_id=layer, label=si.LwwValue(CrdtId(0, 12), "Layer 1"),
                               visible=si.LwwValue(CrdtId(0, 14), not hidden_layer))),
        SceneGroupItemBlock(parent_id=root, item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, layer)),
    ]
    for index, item in enumerate(lines):
        blocks.append(SceneLineItemBlock(parent_id=layer, item=CrdtSequenceItem(CrdtId(1, 20 + index), CrdtId(0, 0), CrdtId(0, 0), 0, item)))
    for index, (x, y, w, h) in enumerate(highlights):
        glyph = si.GlyphRange(start=None, length=5, text="words", color=si.PenColor.HIGHLIGHT, rectangles=[si.Rectangle(x, y, w, h)])
        blocks.append(SceneGlyphItemBlock(parent_id=layer, item=CrdtSequenceItem(CrdtId(1, 60 + index), CrdtId(0, 0), CrdtId(0, 0), 0, glyph)))
    stream = io.BytesIO()
    write_blocks(stream, blocks)
    return stream.getvalue()


def rmdoc(*, pdf: bytes | None = None, file_type: str = "pdf", page_ids: list[str] | None = None,
          redirects: list[int] | None = None, drawings: dict[int, bytes] | None = None, modern: bool = False,
          name: str = "Course", deleted: Sequence[int] = ()) -> tuple[bytes, list[str]]:
    """The bytes of an `.rmdoc`. `drawings` maps a page position to its `.rm` bytes. Returns the bytes and the page ids."""
    count = len(redirects) if redirects is not None else len(page_ids) if page_ids else 3
    ids = page_ids or [str(uuid.uuid4()) for _ in range(count)]
    redirects = redirects if redirects is not None else list(range(len(ids)))
    content: dict[str, Any] = {"fileType": file_type, "formatVersion": 2 if modern else 1, "orientation": "portrait"}
    if modern:
        pages = []
        for i, (page_id, target) in enumerate(zip(ids, redirects, strict=True)):
            entry: dict[str, Any] = {"id": page_id, "idx": {"timestamp": "1:2", "value": chr(ord("a") + i)},
                                     "redir": {"timestamp": "1:2", "value": target}}
            if i in deleted:
                entry["deleted"] = {"timestamp": "1:2", "value": 1}
            pages.append(entry)
        content["cPages"] = {"pages": pages}
    else:
        content["pages"] = ids
        content["redirectionPageMap"] = redirects
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{DOC_ID}.content", json.dumps(content))
        archive.writestr(f"{DOC_ID}.metadata", json.dumps({"visibleName": name, "type": "DocumentType"}))
        if pdf is not None:
            archive.writestr(f"{DOC_ID}.pdf", pdf)
        for position, data in (drawings or {}).items():
            archive.writestr(f"{DOC_ID}/{ids[position]}.rm", data)
    return buffer.getvalue(), ids
