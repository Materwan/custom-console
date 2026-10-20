from __future__ import annotations

import sys
import time

import pytest

from custom_console.core.tools.base import ToolError
from custom_console.core.tools.files import apply_edit
from tests.support.harness import make_world

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixtures")


@pytest.fixture
def w(tmp_path):
    return make_world(tmp_path)


async def read_all(w, name):
    return await w.run("local_read", path=name)


# ---------------------------------------------------------------------- list
async def test_list(w):
    (w.work / "b.txt").write_text("12345")
    (w.work / "a").mkdir()
    (w.work / ".hidden").write_text("")
    text = await w.run("local_list")
    assert text.splitlines()[0].endswith("(2 entries)") and text.splitlines()[1] == "a/"
    assert "b.txt  5B" in text and ".hidden" not in text
    assert ".hidden" in await w.run("local_list", all=True)
    assert "file, 5B" in await w.run("local_list", path="b.txt")
    with pytest.raises(ToolError, match="No such file"):
        await w.run("local_list", path="nope")


# ---------------------------------------------------------------------- read
async def test_read_numbers_lines_without_a_phantom_last_line(w):  # BUG-12
    (w.work / "a.txt").write_text("one\ntwo\n")
    assert await read_all(w, "a.txt") == "1\tone\n2\ttwo"
    (w.work / "empty").write_text("")
    assert "is empty" in await read_all(w, "empty")


async def test_read_in_parts_and_the_guard_only_unlocks_when_all_was_read(w):
    (w.work / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    part = await w.run("local_read", path="big.txt", offset=3, limit=3)
    assert part.startswith("3\tline 3") and "[lines 3-5 of 10; read on with offset=6]" in part
    with pytest.raises(ToolError, match="past the end"):
        await w.run("local_read", path="big.txt", offset=99)
    info = w.ctx.vfs.stat(w.vfs.resolve("big.txt"))
    assert not w.ctx.guard.fully_read(str(w.work / "big.txt"), info)
    await w.run("local_read", path="big.txt", offset=1, limit=2)
    await w.run("local_read", path="big.txt", offset=6, limit=5)
    assert w.ctx.guard.fully_read(str(w.work / "big.txt"), info)


async def test_read_refuses_binary_and_folders_with_advice(w):
    (w.work / "b.bin").write_bytes(b"\x00\x01")
    (w.work / "d").mkdir()
    with pytest.raises(ToolError, match="binary"):
        await read_all(w, "b.bin")
    with pytest.raises(ToolError, match="local_list"):
        await read_all(w, "d")
    with pytest.raises(ToolError, match="No such file"):
        await read_all(w, "none")


async def test_a_huge_line_is_clipped(w):
    (w.work / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    text = await read_all(w, "wide.txt")
    assert "… [+3000 characters]" in text and text.endswith("2\tshort")


# ---------------------------------------------------------------------- search
async def test_search_by_name_with_braces_and_stars(w):  # BUG-14
    for name in ("a.py", "b.md", "c.txt", "sub/d.py"):
        (w.work / name).parent.mkdir(exist_ok=True)
        (w.work / name).write_text("")
    assert (await w.run("local_search", glob="*.{py,md}")).splitlines() == ["a.py", "b.md", "sub/d.py"]
    assert (await w.run("local_search", glob="**/*.py")).splitlines() == ["a.py", "sub/d.py"]
    assert await w.run("local_search", glob="*.zzz") == "No file matches."


async def test_search_text_line_numbers_agree_with_read(w):  # BUG-13
    (w.work / "ff.txt").write_text("one\ntwo\n\x0cthree NEEDLE\nfour\n")  # a form feed must not add a line
    hit = await w.run("local_search", pattern="NEEDLE")
    assert "ff.txt:3:" in hit
    assert "3\t" in await w.run("local_read", path="ff.txt", offset=3, limit=1)


async def test_search_options(w):
    (w.work / "a.txt").write_text("Alpha beta\nGAMMA\nalpha again\n")
    assert (await w.run("local_search", pattern="alpha")).count("a.txt:") == 1
    assert (await w.run("local_search", pattern="alpha", ignore_case=True)).count("a.txt:") == 2
    assert "a.txt:2:GAMMA" in await w.run("local_search", pattern=r"^G\w+$", regex=True)
    assert "[0 matches in 1 files searched]" in await w.run("local_search", pattern="a.b", regex=False)
    with pytest.raises(ToolError, match="not a valid regular expression"):
        await w.run("local_search", pattern="(", regex=True)
    with pytest.raises(ToolError, match="glob"):
        await w.run("local_search")
    capped = await w.run("local_search", pattern="a", ignore_case=True, max_results=1)
    assert "stopped at 1 matches" in capped


async def test_search_skips_secrets_binaries_noise_and_says_so(w):
    (w.work / ".env").write_text("SECRET=NEEDLE\n")
    (w.work / "id_rsa").write_text("NEEDLE")
    (w.work / "blob.bin").write_bytes(b"NEEDLE\x00")
    (w.work / "node_modules").mkdir()
    (w.work / "node_modules" / "m.js").write_text("NEEDLE")
    (w.work / ".git").mkdir()
    (w.work / ".git" / "x").write_text("NEEDLE")
    (w.work / "ok.txt").write_text("NEEDLE here\n")
    text = await w.run("local_search", pattern="NEEDLE")
    assert "ok.txt:1:NEEDLE here" in text and "SECRET" not in text
    assert "2 sensitive files skipped" in text and "1 binary files skipped" in text
    assert "node_modules" not in text and ".git" not in text


async def test_search_does_not_follow_a_link_to_a_secret(w):  # SEC-4 also applies to search
    secret = w.home / ".ssh"
    secret.mkdir()
    (secret / "id_rsa").write_text("NEEDLE")
    (w.work / "innocent.txt").symlink_to(secret / "id_rsa")
    (w.work / "dirlink").symlink_to(secret)
    text = await w.run("local_search", pattern="NEEDLE")
    assert "innocent" not in text.split("[")[0] and "dirlink" not in text.split("[")[0]
    assert "sensitive files skipped" in text


# ---------------------------------------------------------------------- write
async def test_write_creates_files_and_missing_folders_and_is_undoable(w):
    w.undo.begin_turn("t1")
    out = await w.run("local_write", path="new/deep/f.txt", content="a\nb\n")
    assert out == "Created " + f"{w.work}/new/deep/f.txt (2 lines, 4 bytes)." and (w.work / "new/deep/f.txt").read_text() == "a\nb\n"
    report = w.undo.undo_last()
    assert report.deleted and not (w.work / "new").exists()


async def test_overwriting_needs_the_whole_file_read_first(w):  # BUG-6
    f = w.work / "doc.txt"
    f.write_text("".join(f"{i}\n" for i in range(10)))
    with pytest.raises(ToolError, match="not been read in full"):
        await w.run("local_write", path="doc.txt", content="new")
    await w.run("local_read", path="doc.txt", offset=1, limit=3)
    with pytest.raises(ToolError, match="not been read in full"):
        await w.run("local_write", path="doc.txt", content="new")
    await w.run("local_read", path="doc.txt")
    assert "Wrote" in await w.run("local_write", path="doc.txt", content="new\n")
    assert f.read_text() == "new\n"
    assert "Wrote" in await w.run("local_write", path="doc.txt", content="newer\n")  # the console knows what it wrote


async def test_a_file_changed_behind_the_consoles_back_must_be_read_again(w):
    f = w.work / "doc.txt"
    f.write_text("one\n")
    await w.run("local_read", path="doc.txt")
    f.write_text("someone else wrote this\n")
    with pytest.raises(ToolError, match=r"not been read in full|changed"):
        await w.run("local_write", path="doc.txt", content="mine")


async def test_write_keeps_crlf_and_refuses_non_utf8_and_huge_and_folders(w):
    f = w.work / "win.txt"
    f.write_bytes(b"a\r\nb\r\n")
    await w.run("local_read", path="win.txt")
    await w.run("local_write", path="win.txt", content="x\ny\nz\n")
    assert f.read_bytes() == b"x\r\ny\r\nz\r\n"
    bad = w.work / "latin.txt"
    bad.write_bytes("caf\xe9\n".encode("latin-1"))
    await w.run("local_read", path="latin.txt")
    with pytest.raises(ToolError, match="not valid UTF-8"):
        await w.run("local_write", path="latin.txt", content="x")
    with pytest.raises(ToolError, match="over 5,000,000"):
        await w.run("local_write", path="huge.txt", content="x" * 5_000_001)
    (w.work / "d").mkdir()
    with pytest.raises(ToolError, match="is a folder"):
        await w.run("local_write", path="d", content="x")
    from tests.support.memory_backend import MemoryBackend
    w.vfs.resolver.remarkable = MemoryBackend()
    with pytest.raises(ToolError, match="only supported on disks"):
        await w.run("local_write", path="reMarkable:x", content="x")


async def test_writing_the_same_content_changes_nothing(w):
    f = w.work / "same.txt"
    f.write_text("same\n")
    await w.run("local_read", path="same.txt")
    mtime = f.stat().st_mtime_ns
    assert "nothing changed" in await w.run("local_write", path="same.txt", content="same\n")
    assert f.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------- edit
async def test_edit_replaces_exact_text_once(w):
    f = w.work / "e.py"
    f.write_text("a = 1\nb = 2\nc = 1\n")
    with pytest.raises(ToolError, match="not been read in full"):
        await w.run("local_edit", path="e.py", old_text="b = 2", new_text="b = 3")
    await w.run("local_read", path="e.py")
    assert "1 replacement" in await w.run("local_edit", path="e.py", old_text="b = 2", new_text="b = 3")
    assert f.read_text() == "a = 1\nb = 3\nc = 1\n"
    with pytest.raises(ToolError, match="occurs 2 times"):
        await w.run("local_edit", path="e.py", old_text="= 1", new_text="= 9")
    assert "2 replacements" in await w.run("local_edit", path="e.py", old_text="= 1", new_text="= 9", replace_all=True)
    assert f.read_text() == "a = 9\nb = 3\nc = 9\n"
    with pytest.raises(ToolError, match="No such file"):
        await w.run("local_edit", path="none.py", old_text="a", new_text="b")


def test_apply_edit_errors():
    with pytest.raises(ToolError, match="empty"):
        apply_edit("a", "", "b", False)
    with pytest.raises(ToolError, match="same"):
        apply_edit("a", "a", "a", False)
    assert apply_edit("x\ny", "x\r\ny", "z", False) == ("z", 1)  # a CRLF in old_text still matches the normalised text


async def test_edit_keeps_crlf_and_the_bom(w):
    f = w.work / "w.txt"
    f.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    await w.run("local_read", path="w.txt")
    await w.run("local_edit", path="w.txt", old_text="two", new_text="2")
    assert f.read_bytes() == b"\xef\xbb\xbfone\r\n2\r\n"


async def test_a_missed_edit_gives_the_closest_passage_and_is_fast(w):  # PERF-2
    lines = [f"def function_{i}(x):\n    return x * {i}\n" for i in range(6000)]
    f = w.work / "big.py"
    f.write_text("".join(lines))
    await w.run("local_read", path="big.py", limit=1)
    await w.run("local_read", path="big.py", offset=1, limit=20_000)
    wrong = "".join(f"def function_{i}(x):\n    return x * {i} + 1\n" for i in range(3000, 3015))
    begun = time.monotonic()
    with pytest.raises(ToolError) as caught:
        await w.run("local_edit", path="big.py", old_text=wrong, new_text="x")
    assert time.monotonic() - begun < 3.0
    assert "not found" in str(caught.value) and "closest passage starts at line" in str(caught.value)


async def test_the_edit_preview_never_runs_the_closest_passage_search(w):  # PERF-2 / PERF-3
    f = w.work / "p.py"
    f.write_text("".join(f"line {i}\n" for i in range(12_000)))
    spec = w.registry.get("local_edit")
    begun = time.monotonic()
    preview = spec.preview(w.ctx, {"path": "p.py", "old_text": "no such text\n" * 30, "new_text": "x"})
    assert time.monotonic() - begun < 1.0 and "no such text" in preview
    good = spec.preview(w.ctx, {"path": "p.py", "old_text": "line 5\n", "new_text": "LINE 5\n"})
    assert "-line 5" in good and "+LINE 5" in good
