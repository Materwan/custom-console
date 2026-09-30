from custom_console.fs import virtual
from custom_console.fs.virtual import VirtualPath


def test_parse_absolute_remarkable():
    assert virtual.parse("/reMarkable/Notes/Physics", "Ubuntu") == VirtualPath("remarkable", "Notes/Physics")
    assert virtual.parse("/remarkable", "Ubuntu") == VirtualPath("remarkable", "")


def test_parse_is_case_insensitive_and_accepts_backslashes():
    assert virtual.parse("\\REMARKABLE\\a\\b", "Ubuntu") == VirtualPath("remarkable", "a/b")


def test_parse_drive():
    assert virtual.parse("/c:/Users/me", "Ubuntu") == VirtualPath("drive", "Users/me", drive="C:")


def test_parse_wsl_uses_configured_distro():
    assert virtual.parse("/wsl-Debian/home", "Debian") == VirtualPath("wsl", "home")
    assert virtual.parse("/wsl-Ubuntu/home", "Debian") is None


def test_parse_requires_leading_slash_when_absolute():
    assert virtual.parse("reMarkable/x", "Ubuntu") is None
    assert virtual.parse("reMarkable/x", "Ubuntu", absolute=False) == VirtualPath("remarkable", "x")


def test_parse_unknown():
    assert virtual.parse("/foo/bar", "Ubuntu") is None
    assert virtual.parse("/", "Ubuntu") is None


def test_local_path():
    assert virtual.local_path(VirtualPath("drive", "a/b", drive="C:"), "Ubuntu") == "C:/a/b"
    assert virtual.local_path(VirtualPath("drive", "", drive="D:"), "Ubuntu") == "D:/"
    assert virtual.local_path(VirtualPath("wsl", "home"), "Ubuntu") == "//wsl$/Ubuntu/home"


def test_remote_path_is_normalized():
    assert virtual.remote_path(VirtualPath("remarkable", "")) == "/"
    assert virtual.remote_path(VirtualPath("remarkable", "a/../b")) == "/b"


def test_strip_remote_prefix():
    assert virtual.strip_remote_prefix("reMarkable:/Notes") == "/Notes"
    assert virtual.strip_remote_prefix("REMARKABLE:") == "."
    assert virtual.strip_remote_prefix("Notes") is None


def test_local_to_virtual():
    assert virtual.local_to_virtual("C:\\Users\\me") == "/C:/Users/me"
    assert virtual.local_to_virtual("/tmp/x") == "/tmp/x"


def test_root_entries_contain_static_entries():
    entries = virtual.root_entries("Ubuntu")
    assert "reMarkable/" in entries and "wsl-Ubuntu/" in entries
