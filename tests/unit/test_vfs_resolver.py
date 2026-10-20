from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from custom_console.core.vfs.backend import BackendFailure, PathRefused
from custom_console.core.vfs.resolver import DRIVE, POSIX, ROOT, TABLET, WSL, Resolver, Syntax, VPath
from custom_console.platform import for_system
from custom_console.platform.windows import WindowsPaths
from tests.support.memory_backend import MemoryBackend

HOME = VPath(DRIVE, "C", ("Users", "me"))
win = Syntax(True, HOME, WindowsPaths(), lambda: ["Ubuntu", "Debian"])
posix = Syntax(False, VPath(POSIX, "", ("home", "me")), for_system("linux").paths)
ROOT_V = VPath(ROOT)
CWD = VPath(DRIVE, "C", ("Users", "me"))


def w(typed: str, cwd: VPath = CWD) -> str:
    return win.format(win.parse(typed, cwd))


@pytest.mark.parametrize(("typed", "expected"), [
    ("/", "/"), ("", "/C:/Users/me"), (".", "/C:/Users/me"),
    ("C:\\Users\\you", "/C:/Users/you"), ("c:/x", "/C:/x"), ("/c:/x", "/C:/x"), ("C:", "/C:/"), ("C:\\", "/C:/"),
    ("C:foo", "/C:/foo"), ("C:/a/./b//c", "/C:/a/b/c"), ("/C:/a/../b", "/C:/b"),
    ("docs", "/C:/Users/me/docs"), ("docs\\sub", "/C:/Users/me/docs/sub"), ("..", "/C:/Users"), ("../..", "/C:/"),
    ("../../..", "/"), ("../../../..", "/"),
    ("/Users/you", "/C:/Users/you"), ("\\Users\\you", "/C:/Users/you"),
    ("~", "/C:/Users/me"), ("~/docs", "/C:/Users/me/docs"), ("~\\docs", "/C:/Users/me/docs"),
    ("\\\\wsl.localhost\\Ubuntu\\home\\me", "/wsl-Ubuntu/home/me"), ("\\\\wsl$\\ubuntu\\home", "/wsl-Ubuntu/home"),
    ("//wsl.localhost/Debian", "/wsl-Debian/"), ("/wsl-ubuntu/etc", "/wsl-Ubuntu/etc"), ("/wsl-Arch/x", "/wsl-Arch/x"),
    ("\\\\?\\C:\\long\\path", "/C:/long/path"),
    ("reMarkable:Notes", "/reMarkable/Notes"), ("remarkable:/Notes/Plan", "/reMarkable/Notes/Plan"),
    ("/reMarkable/x", "/reMarkable/x"), ("/REMARKABLE", "/reMarkable/"), ("reMarkable:", "/reMarkable/"),
    ("reMarkable:..", "/"),
])
def test_windows_forms(typed, expected):
    assert w(typed) == expected


def test_windows_forms_from_other_places():
    assert w("C:\\x", ROOT_V) == "/C:/x"
    assert w("wsl-Ubuntu", ROOT_V) == "/wsl-Ubuntu/"
    assert w("C:", ROOT_V) == "/C:/"
    assert w("..", ROOT_V) == "/"
    tablet = VPath(TABLET, "", ("A",))
    assert w("reMarkable:B", tablet) == "/reMarkable/A/B"
    assert w("reMarkable:/B", tablet) == "/reMarkable/B"
    assert w("B/C", tablet) == "/reMarkable/A/B/C"
    assert w("reMarkable:B", CWD) == "/reMarkable/B"  # from elsewhere it is the tablet's root
    assert w("..", VPath(WSL, "Ubuntu")) == "/"
    assert w("..", VPath(TABLET)) == "/"


@pytest.mark.parametrize("typed", [
    "\\\\server\\share\\x", "//server/share/x", "\\\\192.168.0.5\\c$", "\\\\?\\UNC\\server\\share", "\\\\wsl.localhost",
])
def test_network_paths_are_refused(typed):
    with pytest.raises(PathRefused):
        win.parse(typed, CWD)


def test_a_rooted_path_with_no_drive_to_stand_on_is_refused():
    with pytest.raises(PathRefused, match="no such drive or mount"):
        win.parse("/Users/you", ROOT_V)


def test_inner_paths():
    assert win.inner(win.parse("C:\\Users\\you", CWD)) == "C:/Users/you"
    assert win.inner(win.parse("/C:/", CWD)) == "C:/"
    assert win.inner(win.parse("/wsl-Ubuntu/home/me", CWD)) == "\\\\wsl.localhost\\Ubuntu\\home\\me"
    assert win.inner(win.parse("reMarkable:Notes/Plan", CWD)) == "/Notes/Plan"
    assert win.inner(VPath(ROOT)) == ""


@pytest.mark.parametrize(("typed", "expected"), [
    ("/", "/"), ("/a/b/../c", "/a/c"), ("..", "/home"), ("../..", "/"), ("../../../..", "/"), ("x/y", "/home/me/x/y"),
    ("~", "/home/me"), ("~/d", "/home/me/d"), ("/a\\b", "/a\\b"), ("a\\b", "/home/me/a\\b"),
    ("reMarkable:Notes", "reMarkable:/Notes"), ("REMARKABLE:/Notes/x", "reMarkable:/Notes/x"), ("//x", "/x"),
])
def test_posix_forms(typed, expected):
    assert posix.format(posix.parse(typed, VPath(POSIX, "", ("home", "me")))) == expected


def test_posix_tablet_relative_and_escape():
    tablet = VPath(TABLET, "", ("A",))
    assert posix.format(posix.parse("B", tablet)) == "reMarkable:/A/B"
    assert posix.format(posix.parse("reMarkable:B", tablet)) == "reMarkable:/A/B"
    assert posix.format(posix.parse("/tmp", tablet)) == "/tmp"  # a rooted path leaves the tablet
    assert posix.format(posix.parse("../..", tablet)) == "reMarkable:/"  # no virtual root on POSIX
    assert posix.inner(posix.parse("reMarkable:Notes", tablet)) == "/A/Notes"


def test_a_backslash_is_an_ordinary_character_on_posix_and_a_separator_on_windows():
    assert posix.parse("a\\b", VPath(POSIX)).parts == ("a\\b",)
    assert win.parse("a\\b", CWD).parts == ("Users", "me", "a", "b")


segments = st.lists(st.sampled_from(["a", "b", "..", ".", "", "C:", "wsl-Ubuntu", "reMarkable", "x y", "~"]), max_size=8)


@given(segments, st.sampled_from(["/", "\\", ""]), st.sampled_from([CWD, ROOT_V, VPath(TABLET, "", ("A",))]))
def test_parsing_is_stable_and_never_leaves_dots_behind(parts, lead, cwd):
    typed = lead + "/".join(parts)
    try:
        first = win.parse(typed, cwd)
    except PathRefused:
        return
    assert all(p not in ("", ".", "..") for p in first.parts)
    again = win.parse(win.format(first), ROOT_V)
    assert again == first


@given(segments, st.sampled_from(["/", ""]))
def test_posix_parsing_is_stable(parts, lead):
    typed = lead + "/".join(parts)
    first = posix.parse(typed, VPath(POSIX, "", ("home", "me")))
    assert all(p not in ("", ".", "..") for p in first.parts)
    assert posix.parse(posix.format(first), VPath(POSIX)) == first


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resolver")
def test_resolver_on_linux(tmp_path):
    resolver = Resolver(for_system("linux"), home=tmp_path)
    here = resolver.resolve("sub/../file.txt", str(tmp_path))
    assert here.virtual == f"{tmp_path}/file.txt" and here.inner == here.virtual and here.backend is not None
    assert resolver.resolve("~/x", "/").virtual == f"{tmp_path}/x"
    assert resolver.start_directory() == os.getcwd()
    with pytest.raises(BackendFailure, match="rmapi"):
        resolver.resolve("reMarkable:Notes", "/")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_real_follows_links_and_keeps_missing_tails(tmp_path):
    (tmp_path / "secret").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "secret")
    resolver = Resolver(for_system("linux"), home=tmp_path)
    through = resolver.resolve("link/new.txt", str(tmp_path))
    assert through.inner.endswith("/link/new.txt")
    assert through.real() == str(Path(os.path.realpath(tmp_path)) / "secret" / "new.txt")


def test_the_windows_root_lists_its_mounts():
    class Fake:
        case_insensitive = True
        def home(self): return Path("C:/Users/me")
        def is_network_path(self, raw): return False
        def is_wsl_path(self, raw): return False
        def credential_dirs(self, home): return ()
        def drives(self): return ["C", "D"]
        def wsl_distros(self): return ["Ubuntu"]

    class FakePlatform:
        name = "windows"
        is_windows = True
        paths = Fake()
        privacy = desktop = None

    resolver = Resolver(FakePlatform())  # type: ignore[arg-type]
    assert [e.name for e in resolver.mounts()] == ["C:", "D:", "wsl-Ubuntu", "reMarkable"]
    assert resolver.resolve("D:\\x", "/").inner == "D:/x"
    assert resolver.resolve("/wsl-Ubuntu/home", "/").backend.name == "wsl"  # type: ignore[union-attr]


# ---------------------------------------------------------------------- the corners the other tests leave
def test_wsl_host_without_a_distribution_is_refused_with_advice():
    with pytest.raises(PathRefused, match="name a WSL distribution"):
        w("\\\\wsl.localhost\\")


def test_a_mount_named_after_a_walk_back_to_the_root():
    assert w("/C:/../D:/x") == "/D:/x"
    assert w("/C:/Users/../../wsl-Ubuntu/etc") == "/wsl-Ubuntu/etc"
    assert w("reMarkable:../C:/y") == "/C:/y"


def test_an_operating_system_path_that_is_a_network_path_is_the_root():
    assert win.from_os_path("\\\\server\\share\\x") == VPath(ROOT)
    assert win.from_os_path("C:\\Users\\me") == HOME
    assert posix.from_os_path("/home/me") == VPath(POSIX, "", ("home", "me"))


def test_the_tablet_has_no_links_to_follow_and_the_root_has_children_named_by_what_is_typed():
    from custom_console.core.vfs.resolver import Resolver as R

    tablet = MemoryBackend()
    linux = R(for_system("linux"), home=Path("/home/me"), remarkable=tablet)
    resolved = linux.resolve("reMarkable:Notes/Plan", "/")
    assert resolved.real() == "/Notes/Plan" and not resolved.on_disk
    windows = R(for_system("windows"), home=Path("C:/Users/me"))
    root = windows.resolve("/", "/")
    assert root.is_root and windows.child(root, "C:").virtual == "/C:/" and windows.child(root, "wsl-Ubuntu").virtual == "/wsl-Ubuntu/"
    assert windows.child(windows.child(root, "C:"), "Users").virtual == "/C:/Users"
