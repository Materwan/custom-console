"""WSL, on the Windows runner only: a real distribution reached through `\\\\wsl.localhost\\<distro>`.

GitHub's Windows runners have `wsl.exe` but usually no installed distribution; then these tests skip. On a machine with one
(a developer's Windows PC) they list the distribution, read from it, and complete a path in it, read-only: nothing is written.
"""

from __future__ import annotations

import sys

import pytest

from custom_console.commands.completion.engine import CompletionEngine
from custom_console.core.vfs.ops import Vfs
from custom_console.core.vfs.resolver import Resolver
from custom_console.platform import for_system

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="WSL exists on Windows only")


@pytest.fixture(scope="module")
def distro():
    platform = for_system("windows")
    found = platform.paths.wsl_distros()
    if not found:
        pytest.skip("no WSL distribution is installed here")
    return found[0]


def test_the_distribution_is_a_mount_and_its_folders_can_be_listed_and_read(distro):
    platform = for_system("windows")
    vfs = Vfs(Resolver(platform), cwd="/")
    mounts = {entry.name for entry in vfs.list(vfs.resolve("/"))}
    assert f"wsl-{distro}" in mounts
    etc = vfs.resolve(f"/wsl-{distro}/etc")
    assert vfs.is_dir(etc) and vfs.stat(etc) is not None
    names = {entry.name for entry in vfs.list(etc)}
    assert "passwd" in names
    text = vfs.read_text(vfs.resolve(f"/wsl-{distro}/etc/passwd")).text
    assert "root" in text


def test_the_unc_form_and_the_virtual_form_are_the_same_place(distro):
    platform = for_system("windows")
    vfs = Vfs(Resolver(platform), cwd="/")
    assert vfs.resolve(f"\\\\wsl.localhost\\{distro}\\etc").virtual == vfs.resolve(f"/wsl-{distro}/etc").virtual
    assert vfs.resolve(f"\\\\wsl$\\{distro}\\etc").virtual == f"/wsl-{distro}/etc"


def test_other_network_paths_are_still_refused_next_to_wsl(distro):
    from custom_console.core.vfs.backend import PathRefused

    vfs = Vfs(Resolver(for_system("windows")), cwd="/")
    with pytest.raises(PathRefused):
        vfs.resolve("\\\\somehost\\share\\file")


def test_a_path_in_the_distribution_is_completed(distro):
    import tempfile
    from pathlib import Path

    from custom_console.commands.shell import new_shell
    from custom_console.paths import DataLayout

    platform = for_system("windows")
    vfs = Vfs(Resolver(platform), cwd="/")
    with tempfile.TemporaryDirectory() as folder:
        shell = new_shell(vfs, platform, DataLayout(Path(folder)), env={})
        engine = CompletionEngine(shell)
        found = {c.text for c in engine.complete(f"cat /wsl-{distro}/etc/pass", len(f"cat /wsl-{distro}/etc/pass")).candidates}
    assert any(text.endswith("passwd") for text in found)
