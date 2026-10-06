"""Builds small but real reMarkable documents for the tests (pymupdf + rmscene)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

DOC_ID = "aef1c475-7e00-4e6b-b399-9c65b51588ad"


def make_pdf(pages: int = 3, width: float = 595, height: float = 842) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for number in range(pages):
        page = document.new_page(width=width, height=height)
        page.insert_text((72, 100), f"Original page {number + 1}")
    data = document.tobytes()
    document.close()
    return data


def make_line(points: Sequence[Tuple[float, float]], color: str = "BLACK", tool: str = "FINELINER_2", width: int = 8):
    from rmscene import scene_items as si

    return si.Line(
        color=si.PenColor[color],
        tool=si.Pen[tool],
        points=[si.Point(x, y, 10, 0, width, 100) for x, y in points],
        thickness_scale=1.0,
        starting_length=0.0,
    )


def make_page(lines: Sequence = (), highlights: Sequence[Tuple[float, float, float, float]] = ()) -> bytes:
    """One ``.rm`` page holding `lines` (and text highlights given as x, y, w, h)."""
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
        scene_items as si,
        write_blocks,
    )

    root, layer = CrdtId(0, 1), CrdtId(0, 11)
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.uuid4()}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=root),
        TreeNodeBlock(si.Group(node_id=root)),
        TreeNodeBlock(si.Group(node_id=layer, label=si.LwwValue(CrdtId(0, 12), "Layer 1"))),
        SceneGroupItemBlock(
            parent_id=root,
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, layer),
        ),
    ]
    for index, line in enumerate(lines):
        blocks.append(
            SceneLineItemBlock(
                parent_id=layer,
                item=CrdtSequenceItem(CrdtId(1, 20 + index), CrdtId(0, 0), CrdtId(0, 0), 0, line),
            )
        )
    for index, (x, y, w, h) in enumerate(highlights):
        glyph = si.GlyphRange(
            start=None, length=5, text="words", color=si.PenColor.HIGHLIGHT, rectangles=[si.Rectangle(x, y, w, h)]
        )
        blocks.append(
            SceneGlyphItemBlock(
                parent_id=layer,
                item=CrdtSequenceItem(CrdtId(1, 60 + index), CrdtId(0, 0), CrdtId(0, 0), 0, glyph),
            )
        )
    stream = io.BytesIO()
    write_blocks(stream, blocks)
    return stream.getvalue()


def make_rmdoc(
    path,
    *,
    pdf: Optional[bytes] = None,
    file_type: str = "pdf",
    page_ids: Optional[List[str]] = None,
    redirects: Optional[List[int]] = None,
    drawings: Optional[Dict[int, bytes]] = None,
    modern: bool = False,
    name: str = "Course",
) -> List[str]:
    """Write an ``.rmdoc`` at `path`. `drawings` maps a page position to its ``.rm`` bytes.
    Returns the page ids."""
    pages = len(redirects) if redirects is not None else (page_ids and len(page_ids)) or 3
    ids = page_ids or [str(uuid.uuid4()) for _ in range(pages)]
    redirects = redirects if redirects is not None else list(range(len(ids)))
    content: dict = {"fileType": file_type, "formatVersion": 1 if not modern else 2, "orientation": "portrait"}
    if modern:
        content["cPages"] = {
            "pages": [
                {"id": page_id, "idx": {"timestamp": "1:2", "value": chr(ord("a") + i)}, "redir": {"timestamp": "1:2", "value": r}}
                for i, (page_id, r) in enumerate(zip(ids, redirects))
            ]
        }
    else:
        content["pages"] = ids
        content["redirectionPageMap"] = redirects
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{DOC_ID}.content", json.dumps(content))
        archive.writestr(f"{DOC_ID}.metadata", json.dumps({"visibleName": name, "type": "DocumentType"}))
        if pdf is not None:
            archive.writestr(f"{DOC_ID}.pdf", pdf)
        for position, data in (drawings or {}).items():
            archive.writestr(f"{DOC_ID}/{ids[position]}.rm", data)
    return ids
