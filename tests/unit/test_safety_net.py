"""The safety net itself. Every test here is itself safe: it only names temp folders, or calls pure functions, or targets paths
that do not exist, so even a broken net could not hurt anything while it is being tested."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.conftest import REAL_HOME
from tests.support import guard
from tests.support.guard import SandboxViolation
from tests.support.harness import SandboxedLocalBackend
from tests.support.shelltest import Sh

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")


def test_the_net_is_installed_and_tests_cannot_see_the_real_home(nobody_home):
    assert guard.active()
    assert Path.home() == nobody_home != REAL_HOME
    assert os.environ["HOME"] == str(nobody_home)
    assert str(REAL_HOME) != os.environ["HOME"] and REAL_HOME.is_dir()


@posix_only
def test_forbidden_is_decided_without_touching_anything():
    home = str(REAL_HOME)
    for path in (home, os.path.dirname(home), "/", "/home", f"{home}/.ssh", f"{home}/anything/at/all", "/usr/lib/x", "/etc"):
        assert guard.forbidden(path), path
    for path in (guard.REPO, f"{guard.REPO}/src", os.path.join(os.path.realpath("/tmp"), "x", "y")):
        assert guard.forbidden(path) is None, path
    with pytest.raises(SandboxViolation, match="real home"):
        guard.check(home)
    assert guard.forbidden(3) is None  # a file descriptor was judged when it was opened


@posix_only
def test_a_link_cannot_smuggle_a_forbidden_path_past_the_guard(tmp_path):
    (tmp_path / "innocent").symlink_to(REAL_HOME)  # a link in a temp folder pointing at the real home
    assert "real home" in (guard.forbidden(tmp_path / "innocent") or "")
    assert guard.forbidden(tmp_path / "innocent" / "x") is not None


@posix_only
def test_os_level_removal_is_guarded_for_the_whole_run(tmp_path):
    nowhere = "/nonexistent-safety-net-probe"      # does not exist: nothing could be hurt even if the guard failed
    for call in (lambda: os.unlink(f"{nowhere}/x"), lambda: os.remove(f"{nowhere}/x"), lambda: os.rmdir(nowhere),
                 lambda: os.rename(nowhere, str(tmp_path / "y")), lambda: os.rename(str(tmp_path / "x"), f"{nowhere}/y"),
                 lambda: os.replace(nowhere, str(tmp_path / "y")), lambda: shutil.rmtree(nowhere),
                 lambda: Path(nowhere).unlink(), lambda: Path(nowhere).rmdir(), lambda: os.removedirs(nowhere)):
        with pytest.raises(SandboxViolation):
            call()
    inside = tmp_path / "f"
    inside.write_text("x")
    inside.unlink()                                   # ordinary work in a temp folder is not in the way
    (tmp_path / "d").mkdir()
    shutil.rmtree(tmp_path / "d")
    assert not inside.exists() and not (tmp_path / "d").exists()


@posix_only
def test_the_guard_does_not_block_directory_descriptor_calls(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").write_text("x")
    shutil.rmtree(tmp_path / "a")  # rmtree works through descriptors inside: only its starting path is judged
    assert not (tmp_path / "a").exists()


def test_a_violation_cannot_be_swallowed_by_the_code_under_test():
    assert issubclass(SandboxViolation, BaseException) and not issubclass(SandboxViolation, Exception)
    try:
        try:
            raise SandboxViolation("x")
        except Exception:  # what a careless handler does
            pytest.fail("an `except Exception` swallowed the violation")
    except SandboxViolation:
        pass


@posix_only
def test_the_sandboxed_backend_refuses_to_change_anything_outside_its_root(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("sandbox")
    decoy_folder = tmp_path_factory.mktemp("decoy")        # inside the allowed temp tree, but NOT inside this sandbox
    decoy = decoy_folder / "precious.txt"
    decoy.write_text("keep me")
    backend = SandboxedLocalBackend(sandbox)
    assert backend.read(str(decoy)) == b"keep me"            # reading anywhere is harmless
    for call in (lambda: backend.remove(str(decoy)), lambda: backend.write(str(decoy), b"x"), lambda: backend.touch(str(decoy)),
                 lambda: backend.rename(str(decoy), str(sandbox / "stolen")), lambda: backend.rename(str(sandbox / "a"), str(decoy)),
                 lambda: backend.mkdir(str(decoy_folder / "new")), lambda: backend.rmdir(str(decoy_folder))):
        with pytest.raises(SandboxViolation):
            call()
    assert decoy.read_text() == "keep me" and not (decoy_folder / "new").exists()
    # inside the root everything works, and a link to the outside can be removed as a link but not written through
    (sandbox / "link").symlink_to(decoy)
    with pytest.raises(SandboxViolation):
        backend.write(str(sandbox / "link"), b"overwrite")
    assert decoy.read_text() == "keep me"
    backend.remove(str(sandbox / "link"))
    assert not (sandbox / "link").is_symlink() and decoy.exists()
    backend.write(str(sandbox / "ok"), b"fine")
    assert (sandbox / "ok").read_bytes() == b"fine"


@posix_only
def test_even_with_the_protection_switched_off_a_shell_cannot_reach_outside(tmp_path_factory, monkeypatch):
    """Simulates the bug that deleted a real home: the protection of home is gone. The disk must still hold."""
    from custom_console.core.vfs.ops import Vfs

    decoy_folder = tmp_path_factory.mktemp("decoy")
    (decoy_folder / "inner").mkdir()
    (decoy_folder / "inner" / "precious.txt").write_text("keep me")
    sh = Sh(tmp_path_factory.mktemp("world"))
    monkeypatch.setattr(Vfs, "protected_reason", lambda self, resolved: None)
    with pytest.raises(SandboxViolation):
        sh(f"rm -rf {decoy_folder}")                   # a folder outside the sandbox: refused by the disk itself
    with pytest.raises(SandboxViolation):
        sh(f"mv {sh.work} {decoy_folder}/stolen")
    assert (decoy_folder / "inner" / "precious.txt").read_text() == "keep me" and sh.work.is_dir()
    # and `rm -rf ~` with no protection removes the sandbox's own home, never anything else
    assert sh.inside_sandbox("~")
    sh("rm -rf ~")
    assert not sh.home.exists() and decoy_folder.exists() and REAL_HOME.is_dir()


def test_the_shell_harness_refuses_to_exist_without_the_net(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_installed", False)
    with pytest.raises(AssertionError, match="safety net"):
        Sh(tmp_path)


def test_a_sandboxed_shell_resolves_home_inside_the_sandbox(tmp_path):
    sh = Sh(tmp_path)
    assert sh.inside_sandbox("~") and sh.inside_sandbox(".") and not sh.inside_sandbox("/")
    assert sh("echo ~").out == [str(sh.home)]
