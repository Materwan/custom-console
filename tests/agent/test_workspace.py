from __future__ import annotations

import pytest

from custom_console.agent.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace({"result": tmp_path / "result", "tmp": tmp_path / "tmp"})


class TestSandbox:
    def test_roots_are_created_on_demand(self, ws, tmp_path):
        assert ws.root("result") == (tmp_path / "result").resolve()
        assert (tmp_path / "result").is_dir()

    def test_unknown_directory(self, ws):
        with pytest.raises(ValueError, match="Unknown workspace directory"):
            ws.resolve("etc", "passwd")

    @pytest.mark.parametrize(
        "bad",
        ["../secret.txt", "../../etc/passwd", "sub/../../x", "..", "C:/Windows/system.ini", "/etc/passwd", "\\\\server\\share"],
    )
    def test_escapes_are_refused(self, ws, bad):
        with pytest.raises(ValueError, match="escapes"):
            ws.resolve("result", bad)

    def test_dot_dot_inside_the_workspace_is_fine(self, ws):
        assert ws.resolve("result", "a/../b.txt") == ws.root("result") / "b.txt"

    def test_sibling_folder_with_same_prefix_is_not_inside(self, ws, tmp_path):
        (tmp_path / "result_evil").mkdir()
        with pytest.raises(ValueError):
            ws.resolve("result", "../result_evil/x")

    def test_symlink_pointing_outside_is_refused(self, ws, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = ws.root("result") / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted")
        with pytest.raises(ValueError, match="escapes"):
            ws.resolve("result", "link/secret.txt")
        with pytest.raises(ValueError):
            ws.read("result", "link/secret.txt")


class TestOperations:
    def test_write_read_roundtrip_creates_folders(self, ws):
        path = ws.write("result", "notes/day1/a.txt", "héllo")
        assert path.read_text(encoding="utf-8") == "héllo"
        assert ws.read("result", "notes/day1/a.txt") == ("héllo", False)

    def test_read_truncates(self, ws):
        ws.write("tmp", "big.txt", "x" * 100)
        assert ws.read("tmp", "big.txt", max_chars=10) == ("x" * 10, True)

    def test_overwrite_false(self, ws):
        ws.write("tmp", "a.txt", "1")
        with pytest.raises(FileExistsError):
            ws.write("tmp", "a.txt", "2", overwrite=False)
        ws.write("tmp", "a.txt", "3")
        assert ws.read("tmp", "a.txt")[0] == "3"

    def test_write_over_a_directory(self, ws):
        ws.write("tmp", "d/x.txt", "1")
        with pytest.raises(IsADirectoryError):
            ws.write("tmp", "d", "1")

    def test_read_errors(self, ws):
        ws.write("tmp", "d/x.txt", "1")
        with pytest.raises(IsADirectoryError):
            ws.read("tmp", "d")
        with pytest.raises(FileNotFoundError):
            ws.read("tmp", "missing.txt")

    def test_list_sorted_with_directory_marker(self, ws):
        ws.write("tmp", "b.txt", "1")
        ws.write("tmp", "A.txt", "1")
        ws.write("tmp", "dir/x", "1")
        assert ws.list("tmp") == ["A.txt", "b.txt", "dir/"]
        assert ws.list("tmp", "dir") == ["x"]
        with pytest.raises(NotADirectoryError):
            ws.list("tmp", "b.txt")

    def test_delete_file_and_directory(self, ws):
        ws.write("tmp", "d/x.txt", "1")
        ws.write("tmp", "f.txt", "1")
        ws.delete("tmp", "f.txt")
        ws.delete("tmp", "d")
        assert ws.list("tmp") == []
        with pytest.raises(FileNotFoundError):
            ws.delete("tmp", "f.txt")

    def test_root_cannot_be_deleted_or_moved(self, ws):
        for bad in (".", "", "a/.."):
            with pytest.raises(ValueError, match="root"):
                ws.delete("tmp", bad)
        with pytest.raises(ValueError, match="root"):
            ws.move("tmp", ".", "result", "x")

    def test_move_between_workspaces(self, ws):
        ws.write("tmp", "draft.txt", "data")
        destination = ws.move("tmp", "draft.txt", "result", "final/report.txt")
        assert destination.read_text() == "data"
        assert ws.list("tmp") == []
        with pytest.raises(FileNotFoundError):
            ws.move("tmp", "draft.txt", "result", "again.txt")

    def test_move_cannot_leave_the_sandbox(self, ws):
        ws.write("tmp", "a.txt", "1")
        with pytest.raises(ValueError):
            ws.move("tmp", "a.txt", "result", "../../elsewhere.txt")
        assert ws.list("tmp") == ["a.txt"]

    def test_describe(self, ws, tmp_path):
        assert ws.describe(ws.write("result", "a/b.txt", "x")) == "result/a/b.txt"
        assert ws.describe(ws.root("tmp")) == "tmp"
        assert ws.describe(tmp_path / "elsewhere") == str(tmp_path / "elsewhere")
