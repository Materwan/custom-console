from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from custom_console.agent.checkpoints import Checkpoints, SnapshotTooLargeError
from custom_console.agent.zone import FreeZone


# --------------------------------------------------------------------------- #
# FreeZone
# --------------------------------------------------------------------------- #


class TestFreeZone:
    def test_contains_the_folder_and_its_descendants_only(self, tmp_path):
        zone = FreeZone(tmp_path / "proj")
        (tmp_path / "proj" / "sub").mkdir(parents=True)
        assert zone.contains(tmp_path / "proj")
        assert zone.contains(tmp_path / "proj" / "sub" / "x.txt")  # not existing yet
        assert not zone.contains(tmp_path)
        assert not zone.contains(tmp_path / "proj2")
        assert not zone.contains(tmp_path / "proj" / ".." / "other")

    def test_strict_excludes_the_zone_folder_itself(self, tmp_path):
        zone = FreeZone(tmp_path)
        assert zone.contains(tmp_path) and not zone.contains(tmp_path, strict=True)
        assert zone.contains(tmp_path / "a", strict=True)

    @pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
    def test_comparison_ignores_case_on_windows(self, tmp_path):
        assert FreeZone(tmp_path).contains(str(tmp_path).upper() + "\\x")

    def test_empty_zone_contains_nothing(self, tmp_path):
        zone = FreeZone(reason="because")
        assert not zone.active and not zone.contains(tmp_path) and "because" in zone.describe()

    def test_around_accepts_a_project_folder(self, tmp_path):
        zone = FreeZone.around(tmp_path)
        assert zone.active and zone.root == Path(os.path.realpath(tmp_path))

    @pytest.mark.parametrize("where", ["home", "home_parent", "anchor"])
    def test_around_refuses_folders_that_are_too_wide(self, where):
        home = Path.home()
        path = {"home": home, "home_parent": home.parent, "anchor": Path(home.anchor)}[where]
        zone = FreeZone.around(path)
        assert not zone.active and "too wide" in zone.reason

    def test_around_needs_a_real_local_folder(self, tmp_path):
        assert not FreeZone.around(None).active
        assert "not a local folder" in FreeZone.around(tmp_path / "missing").reason


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #


@pytest.fixture
def cp(tmp_path):
    return Checkpoints(tmp_path / "checkpoints")


class TestCheckpoints:
    def test_modified_file_is_restored(self, cp, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("before")
        cp.begin_turn("t")
        cp.backup(target)
        target.write_text("after")
        assert cp.undo() == [f"restored {target}"]
        assert target.read_text() == "before"

    def test_created_file_is_removed(self, cp, tmp_path):
        target = tmp_path / "new.txt"
        cp.begin_turn("t")
        cp.backup(target)  # did not exist
        target.write_text("x")
        assert cp.undo() == [f"removed {target}"] and not target.exists()

    def test_deleted_folder_comes_back_with_its_content(self, cp, tmp_path):
        folder = tmp_path / "d"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "f.txt").write_text("content")
        cp.begin_turn("t")
        cp.backup(folder)
        import shutil

        shutil.rmtree(folder)
        cp.undo()
        assert (folder / "sub" / "f.txt").read_text() == "content"

    def test_a_move_is_reversed(self, cp, tmp_path):
        source, destination = tmp_path / "a.txt", tmp_path / "b.txt"
        source.write_text("x")
        cp.begin_turn("t")
        source.rename(destination)
        cp.moved(source, destination)
        cp.undo()
        assert source.read_text() == "x" and not destination.exists()

    def test_undo_does_not_overwrite_what_reappeared_at_the_origin(self, cp, tmp_path):
        source, destination = tmp_path / "a.txt", tmp_path / "b.txt"
        destination.write_text("moved")
        cp.begin_turn("t")
        cp.moved(source, destination)
        source.write_text("new occupant")
        assert "exists again" in cp.undo()[0]
        assert source.read_text() == "new occupant" and destination.read_text() == "moved"

    def test_only_the_first_state_of_a_turn_is_kept(self, cp, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("v0")
        cp.begin_turn("t")
        cp.backup(target)
        target.write_text("v1")
        cp.backup(target)  # second edit in the same turn
        target.write_text("v2")
        assert len(cp.undo()) == 1 and target.read_text() == "v0"

    def test_turns_are_undone_one_at_a_time_most_recent_first(self, cp, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("v0")
        for label, content in (("first", "v1"), ("second", "v2")):
            cp.begin_turn(label)
            cp.backup(target)
            target.write_text(content)
        assert cp.available == 2
        cp.undo()
        assert target.read_text() == "v1"
        cp.undo()
        assert target.read_text() == "v0"
        with pytest.raises(LookupError):
            cp.undo()

    def test_turns_without_changes_are_skipped(self, cp, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("v0")
        cp.begin_turn("changes")
        cp.backup(target)
        target.write_text("v1")
        cp.begin_turn("only reading")
        cp.begin_turn("still nothing")
        assert cp.available == 1
        cp.undo()
        assert target.read_text() == "v0"

    def test_nothing_to_undo(self, cp):
        with pytest.raises(LookupError):
            cp.undo()

    def test_large_content_is_refused_rather_than_changed_without_a_safety_net(self, tmp_path):
        cp = Checkpoints(tmp_path / "cp", limit=10)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 100)
        cp.begin_turn("t")
        with pytest.raises(SnapshotTooLargeError):
            cp.backup(big)
        assert big.read_bytes() == b"x" * 100

    def test_backup_files_are_released_after_undo(self, cp, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("v0")
        cp.begin_turn("t")
        cp.backup(target)
        cp.undo()
        assert not any(cp.directory.rglob("a.txt"))

    def test_old_sessions_are_pruned(self, tmp_path):
        root = tmp_path / "cp"
        old = root / "20200101-000000"
        old.mkdir(parents=True)
        (old / "x").write_text("x")
        ancient = time.time() - 30 * 86400
        os.utime(old, (ancient, ancient))
        recent = root / "20990101-000000"
        recent.mkdir()
        Checkpoints(root)
        assert not old.exists() and recent.exists()
