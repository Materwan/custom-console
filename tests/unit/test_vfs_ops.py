from __future__ import annotations

import os
import sys

import pytest

from custom_console.core.vfs.backend import (
    AlreadyExists,
    IsADirectory,
    NotADirectory,
    NotFound,
    PathRefused,
    Unsupported,
)
from custom_console.core.vfs.ops import BinaryFile, Vfs
from custom_console.platform import Cancelled, CancelToken
from tests.support.harness import sandbox_resolver
from tests.support.memory_backend import MemoryBackend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in the fixtures")


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    tablet = MemoryBackend()
    tablet.dirs |= {"/Notes"}
    resolver = sandbox_resolver(tmp_path, home, remarkable=tablet)
    vfs = Vfs(resolver, cwd=str(work))
    return vfs, work, tablet


def w(vfs, name, text="x\n"):
    r = vfs.resolve(name)
    vfs.write_bytes(r, text.encode())
    return r


def test_write_read_list_stat(world):
    vfs, work, _ = world
    w(vfs, "a.txt", "hello\r\nworld\r\n")
    assert (work / "a.txt").read_bytes() == b"hello\r\nworld\r\n"
    text = vfs.read_text(vfs.resolve("a.txt"))
    assert text.text == "hello\nworld\n" and not text.truncated and text.size == 14
    assert [e.name for e in vfs.list(vfs.here())] == ["a.txt"]
    assert vfs.stat(vfs.resolve("a.txt")).size == 14
    assert vfs.stat(vfs.resolve("nope")) is None
    assert vfs.read_text(vfs.resolve("a.txt"), max_bytes=5).truncated


def test_binary_directories_and_missing(world):
    vfs, work, _ = world
    (work / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(BinaryFile):
        vfs.read_text(vfs.resolve("b.bin"))
    (work / "d").mkdir()
    with pytest.raises(IsADirectory):
        vfs.read_bytes(vfs.resolve("d"))
    with pytest.raises(NotFound):
        vfs.read_bytes(vfs.resolve("none"))
    with pytest.raises(NotADirectory):
        w(vfs, "f")
        vfs.list(vfs.resolve("f"))


def test_chdir_is_per_vfs_and_never_touches_the_process(world):
    vfs, work, _ = world
    (work / "sub").mkdir()
    process_cwd = os.getcwd()
    vfs.chdir("sub")
    assert vfs.cwd == f"{work}/sub" and os.getcwd() == process_cwd
    vfs.chdir("..")
    assert vfs.cwd == str(work)
    with pytest.raises(NotFound):
        vfs.chdir("nope")
    w(vfs, "f")
    with pytest.raises(NotADirectory):
        vfs.chdir("f")


def test_mkdir_touch_rmdir_remove(world):
    vfs, work, _ = world
    vfs.mkdir(vfs.resolve("a/b/c"), parents=True)
    assert (work / "a" / "b" / "c").is_dir()
    with pytest.raises(AlreadyExists):
        vfs.mkdir(vfs.resolve("a"))
    vfs.touch(vfs.resolve("t"))
    assert (work / "t").exists()
    with pytest.raises(IsADirectory, match="-r"):
        vfs.remove(vfs.resolve("a"))
    vfs.remove(vfs.resolve("t"))
    with pytest.raises(NotFound):
        vfs.remove(vfs.resolve("t"))
    with pytest.raises(NotADirectory):
        w(vfs, "f")
        vfs.rmdir(vfs.resolve("f"))
    vfs.rmdir(vfs.resolve("a/b/c"))
    assert not (work / "a" / "b" / "c").exists()


def test_symlinks_are_removed_not_followed(world):
    vfs, work, _ = world
    target = work / "real"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (work / "link").symlink_to(target)
    vfs.remove_tree(vfs.resolve("link"))
    assert not (work / "link").exists() and (target / "keep.txt").read_text() == "keep"


def test_remove_tree_goes_on_after_a_failure_and_reports_it(world):
    vfs, work, _ = world
    tree = work / "t"
    (tree / "locked").mkdir(parents=True)
    (tree / "locked" / "inside.txt").write_text("x")
    (tree / "free.txt").write_text("y")
    os.chmod(tree / "locked", 0o500)  # cannot delete what is inside
    try:
        report = vfs.remove_tree(vfs.resolve("t"))
    finally:
        os.chmod(tree / "locked", 0o700)
    assert not report.ok and any("locked" in p for p, _ in report.failed)
    assert not (tree / "free.txt").exists()  # the rest was removed


def test_protected_places(world):
    """SAFETY: nothing that removes or moves is ever given a real path here. `/` and the tablet root are judged by the pure
    `protected_reason`; everything that is executed lies inside the temp tree (and the backend would refuse otherwise)."""
    vfs, work, _ = world
    sandbox = work.parent.parent
    for typed in ("/", "reMarkable:/"):
        assert vfs.protected_reason(vfs.resolve(typed)), typed
    for typed in ("~", "..", ".", str(work.parent), str(sandbox)):
        with pytest.raises(PathRefused):
            vfs.remove_tree(vfs.resolve(typed))
    (work / "sub").mkdir()
    vfs.chdir("sub")
    with pytest.raises(PathRefused, match="current folder"):
        vfs.remove_tree(vfs.resolve(str(work)))
    with pytest.raises(PathRefused):
        vfs.move(vfs.resolve(str(work)), vfs.resolve(str(sandbox / "elsewhere")))
    with pytest.raises(PathRefused):
        vfs.rmdir(vfs.resolve(str(work)))


def test_a_path_that_resolves_to_home_is_refused_but_a_link_to_it_can_go(world):
    vfs, work, _ = world
    sandbox = work.parent.parent
    home = work.parent
    (sandbox / "alias").symlink_to(sandbox)        # alias/home is home, reached through a link in the middle
    with pytest.raises(PathRefused, match="home"):
        vfs.remove_tree(vfs.resolve(str(sandbox / "alias" / "home")))
    (sandbox / "shortcut").symlink_to(home)         # a link TO home: the link goes, home stays
    vfs.remove_tree(vfs.resolve(str(sandbox / "shortcut")))
    assert not (sandbox / "shortcut").exists() and not (sandbox / "shortcut").is_symlink() and work.is_dir()
    vfs.remove_tree(vfs.resolve(str(sandbox / "alias")))       # a link to the folder holding home goes as a link too
    assert not (sandbox / "alias").is_symlink() and home.is_dir()


def test_copy_refuses_to_overwrite_without_force_and_checks_what_it_wrote(world):
    vfs, work, _ = world
    w(vfs, "a", "one")
    w(vfs, "b", "two")
    report = vfs.copy(vfs.resolve("a"), vfs.resolve("b"))
    assert not report.ok and "-f" in report.failed[0][1] and (work / "b").read_text() == "two"
    assert vfs.copy(vfs.resolve("a"), vfs.resolve("b"), force=True).ok and (work / "b").read_text() == "one"
    (work / "dir").mkdir()
    vfs.copy(vfs.resolve("a"), vfs.resolve("dir"))
    assert (work / "dir" / "a").read_text() == "one"
    with pytest.raises(IsADirectory):
        vfs.copy(vfs.resolve("dir"), vfs.resolve("dir2"))


def test_recursive_copy_gives_a_result_per_file_and_never_silently_drops(world):
    vfs, work, tablet = world
    for name in ("one", "two", "three"):
        w(vfs, f"src/{name}.txt") if (work / "src").exists() else (vfs.mkdir(vfs.resolve("src")), w(vfs, f"src/{name}.txt"))
    vfs.mkdir(vfs.resolve("src/sub"))
    w(vfs, "src/sub/four.txt")
    tablet.fail_on.add("/Notes/src/two.txt")
    report = vfs.copy(vfs.resolve("src"), vfs.resolve("reMarkable:Notes"), recursive=True)
    assert report.copied == 3 and len(report.failed) == 1 and "refused" in report.failed[0][1]
    assert report.failed[0][0].endswith("two.txt")
    assert sorted(p for p in tablet.files) == ["/Notes/src/one.txt", "/Notes/src/sub/four.txt", "/Notes/src/three.txt"]


def test_a_short_copy_is_caught(world):
    vfs, _, tablet = world
    w(vfs, "a", "abcdef")
    tablet.short_write.add("/Notes/a")
    report = vfs.copy(vfs.resolve("a"), vfs.resolve("reMarkable:Notes"))
    assert not report.ok and "5 bytes, expected 6" in report.failed[0][1]


def test_cannot_copy_a_folder_into_itself(world):
    vfs, work, _ = world
    (work / "d").mkdir()
    with pytest.raises(PathRefused):
        vfs.copy(vfs.resolve("d"), vfs.resolve("d/inner"), recursive=True)


def test_move_renames_and_refuses_to_overwrite(world):
    vfs, work, _ = world
    w(vfs, "a", "one")
    w(vfs, "b", "two")
    vfs.move(vfs.resolve("a"), vfs.resolve("c"))
    assert not (work / "a").exists() and (work / "c").read_text() == "one"
    with pytest.raises(AlreadyExists, match="-f"):
        vfs.move(vfs.resolve("c"), vfs.resolve("b"))
    vfs.move(vfs.resolve("c"), vfs.resolve("b"), force=True)
    assert (work / "b").read_text() == "one"
    (work / "dir").mkdir()
    vfs.move(vfs.resolve("b"), vfs.resolve("dir"))
    assert (work / "dir" / "b").exists()
    with pytest.raises(PathRefused, match="itself"):
        vfs.move(vfs.resolve("dir"), vfs.resolve("dir/x"))


def test_move_across_backends_copies_checks_then_deletes(world):
    vfs, work, tablet = world
    w(vfs, "a", "payload")
    vfs.move(vfs.resolve("a"), vfs.resolve("reMarkable:Notes"))
    assert tablet.files["/Notes/a"] == b"payload" and not (work / "a").exists()
    w(vfs, "bad", "payload")
    tablet.short_write.add("/Notes/bad")
    report = vfs.move(vfs.resolve("bad"), vfs.resolve("reMarkable:Notes"))
    assert not report.ok and (work / "bad").read_text() == "payload"  # the source stays when the copy is wrong


def test_unsupported_operations_give_one_clear_message(world):
    vfs, _, tablet = world
    with pytest.raises(Unsupported, match="cannot touch files"):
        vfs.touch(vfs.resolve("reMarkable:Notes/x"))
    tablet.caps = frozenset({"list", "stat"})
    with pytest.raises(Unsupported, match="cannot be read"):
        vfs.read_bytes(vfs.resolve("reMarkable:Notes/x"))


def test_walk_lists_files_first_then_folders_and_survives_unreadable_ones(world):
    vfs, work, _ = world
    (work / "z.txt").write_text("")
    (work / "a.txt").write_text("")
    (work / "sub" / "deep").mkdir(parents=True)
    (work / "sub" / "in.txt").write_text("")
    (work / "locked").mkdir()
    os.chmod(work / "locked", 0)
    errors = []
    try:
        names = [(x.entry.name, x.depth) for x in vfs.walk(vfs.here(), on_error=lambda r, e: errors.append(r.virtual))]
    finally:
        os.chmod(work / "locked", 0o700)
    assert names == [("a.txt", 1), ("z.txt", 1), ("locked", 1), ("sub", 1), ("in.txt", 2), ("deep", 2)]
    assert errors == [f"{work}/locked"]
    limited = [x.entry.name for x in vfs.walk(vfs.here(), max_depth=1)]
    assert "in.txt" not in limited
    assert "a.txt" not in [x.entry.name for x in vfs.walk(vfs.here(), include_hidden=False) if x.entry.name.startswith(".")]


def test_a_cancelled_walk_and_copy_stop(world):
    vfs, work, _ = world
    for i in range(5):
        (work / f"f{i}").write_text("x")
    vfs.cancel = CancelToken()
    vfs.cancel.cancel()
    with pytest.raises(Cancelled):
        list(vfs.walk(vfs.here()))
    with pytest.raises(Cancelled):
        vfs.copy(vfs.resolve("f1"), vfs.resolve("g1"))


def test_writes_are_atomic_and_keep_the_mode(world):
    vfs, work, _ = world
    path = work / "run.sh"
    path.write_text("old")
    os.chmod(path, 0o750)
    vfs.write_bytes(vfs.resolve("run.sh"), b"new")
    assert path.read_text() == "new" and (path.stat().st_mode & 0o777) == 0o750
    assert [p.name for p in work.iterdir()] == ["run.sh"]  # no temp file left
    (work / "link").symlink_to(path)
    vfs.write_bytes(vfs.resolve("link"), b"through")
    assert (work / "link").is_symlink() and path.read_text() == "through"


def test_a_read_only_file_is_removed_on_windows_after_its_flag_is_cleared(world, monkeypatch):
    """BUG-19. On Windows `os.unlink` refuses a read-only file until the flag is cleared; here the first call refuses."""
    from custom_console.core.vfs import local as local_module

    vfs, work, _ = world
    target = work / "locked.txt"
    target.write_text("x")
    target.chmod(0o400)
    real_unlink, calls = os.unlink, []

    def refuses_once(path):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        real_unlink(path)

    monkeypatch.setattr(local_module, "WINDOWS", True)
    monkeypatch.setattr(local_module.os, "unlink", refuses_once)
    vfs.remove(vfs.resolve("locked.txt"))
    assert not target.exists() and len(calls) == 2


def test_the_same_refusal_on_posix_is_a_clear_error_not_a_forced_delete(world, monkeypatch):
    from custom_console.core.vfs import local as local_module
    from custom_console.core.vfs.backend import PermissionDenied

    vfs, work, _ = world
    (work / "kept.txt").write_text("x")

    def always_refuses(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(local_module, "WINDOWS", False)
    monkeypatch.setattr(local_module.os, "unlink", always_refuses)
    with pytest.raises(PermissionDenied):
        vfs.remove(vfs.resolve("kept.txt"))
    assert (work / "kept.txt").exists()
