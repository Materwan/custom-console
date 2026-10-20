from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from custom_console.platform import Cancelled, CancelToken, for_system
from custom_console.platform.linux import exec_argv, find_desktop_entry, read_desktop_entry
from custom_console.platform.process import (
    HeadTail,
    StartError,
    decode,
    run_attached,
    run_capture,
    start_background,
)
from custom_console.platform.windows import WindowsPaths, acl_problem, parse_sddl, rights_mask

PY = sys.executable
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")

ME = "S-1-5-21-4030015217-2861353063-1598246050-1001"
MACHINE = "S-1-5-21-4030015217-2861353063-1598246050"
# what `icacls <token> /save` wrote on the PC where the console refused to start: the user's own SID and the computer's SID
# (the computer is named like the user, so `icacls ... /grant erwan:F` had granted the computer as well)
SDDL_REAL = f"D:PAI(A;;FA;;;{ME})(A;;FA;;;{MACHINE})"
SDDL_OK = f"D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{ME})"
SDDL_FOLDER = f"D:PAI(A;OICI;FA;;;{ME})(A;OICIID;FA;;;SY)(D;;FW;;;WD)(A;OICIID;0x1200a9;;;S-1-5-80-956008885-3418522649-1831038044)"


def test_the_access_list_is_judged_by_sid_not_by_name():
    path = Path("C:/Users/erwan/AppData/Roaming/clara/token")
    assert parse_sddl(SDDL_REAL) == [(ME, 0x1F01FF), (MACHINE, 0x1F01FF)]
    assert acl_problem(path, parse_sddl(SDDL_OK), ME) is None
    problem = acl_problem(path, parse_sddl(SDDL_REAL), ME)       # the computer's own SID is not you
    assert problem is not None and MACHINE in problem and f"/grant:r \"*{ME}:F\"" in problem and f"/remove \"*{MACHINE}\"" in problem


def test_other_people_and_groups_are_found_whatever_language_their_names_are_in():
    path = Path("C:/x/token")
    for sddl, name in ((f"D:P(A;;FA;;;{ME})(A;;FR;;;WD)", "Everyone"), (f"D:P(A;;FA;;;{ME})(A;;0x1200a9;;;BU)", "Users"),
                       (f"D:P(A;;FA;;;{ME})(A;;GR;;;AU)", "Authenticated Users"), (f"D:P(A;;FA;;;{ME})(A;;FRFX;;;S-1-5-21-1-2-3-1002)", "S-1-5-21-1-2-3-1002")):
        problem = acl_problem(path, parse_sddl(sddl), ME)
        assert problem is not None and f"read by {name}:" in problem, sddl


def test_what_is_not_a_read_and_what_is_not_an_allow_is_left_alone():
    path = Path("C:/x/token")
    for sddl in (f"D:P(A;;FA;;;{ME})(D;;FA;;;WD)",              # a deny grants nothing
                 f"D:P(A;;FA;;;{ME})(A;;FW;;;BU)",              # write-only: not a read
                 f"D:P(A;;FA;;;{ME})(A;;SD;;;WD)",              # delete only
                 f"D:P(A;;FA;;;{ME})(A;;0x120116;;;WD)"):       # the mask of FW
        assert acl_problem(path, parse_sddl(sddl), ME) is None, sddl
    assert acl_problem(path, parse_sddl(SDDL_FOLDER), ME) is None  # inherited system, a deny, the system installer
    assert acl_problem(path, parse_sddl("D:P(A;;0xzz;;;WD)"), ME) is not None  # a mask it cannot read counts as readable
    assert parse_sddl("") == [] and rights_mask("FRFX") == 0x120089 | 0x1200A0 and rights_mask("GR") == 0x80000000


@pytest.mark.parametrize(("raw", "network", "wsl"), [
    ("\\\\server\\share\\x", True, False), ("//server/share/x", True, False),
    ("\\\\wsl$\\Ubuntu\\home", False, True), ("\\\\wsl.localhost\\Ubuntu\\home", False, True),
    ("//WSL.LOCALHOST/Ubuntu/home", False, True), ("C:\\Users\\me", False, False), ("\\\\?\\C:\\long\\path", False, False),
    ("\\\\?\\UNC\\server\\share", True, False), ("\\\\.\\COM1", False, False), ("relative\\path", False, False),
])
def test_windows_network_and_wsl_paths(raw, network, wsl):
    rules = WindowsPaths()
    assert rules.is_network_path(raw) is network and rules.is_wsl_path(raw) is wsl


def test_credential_dirs_per_platform():
    home = Path("/h")
    assert Path("/h/.ssh") in for_system("linux").paths.credential_dirs(home)
    assert Path("/h/Library/Keychains") in for_system("macos").paths.credential_dirs(home)
    assert any(p.name == "User Data" for p in for_system("windows").paths.credential_dirs(home))
    assert for_system("windows").paths.case_insensitive and not for_system("linux").paths.case_insensitive


@posix_only
def test_posix_privacy(tmp_path):
    privacy = for_system("linux").privacy
    secret = tmp_path / "token"
    privacy.write_private(secret, "clara_a_b\n")
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600 and secret.read_text() == "clara_a_b\n"
    assert privacy.problem(secret) is None
    os.chmod(secret, 0o644)
    assert "chmod 600" in (privacy.problem(secret) or "")
    privacy.make_private(secret)
    assert privacy.problem(secret) is None
    folder = tmp_path / "d"
    folder.mkdir()
    os.chmod(folder, 0o755)
    assert "chmod 700" in (privacy.problem(folder) or "")
    assert "Cannot check" in (privacy.problem(tmp_path / "missing") or "")


@posix_only
def test_write_private_replaces_atomically_and_leaves_no_temp_files(tmp_path):
    privacy = for_system("linux").privacy
    target = tmp_path / "sub" / "token"
    privacy.write_private(target, "one")
    privacy.write_private(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["token"]


def test_desktop_entries(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "org.gnome.gedit.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Text Editor\nName[fr]=Éditeur\nExec=gedit --new-window %U\n"
        "[Desktop Action new]\nName=New\nExec=other\n")
    (apps / "hidden.desktop").write_text("[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\nExec=h\n")
    entry = read_desktop_entry(apps / "org.gnome.gedit.desktop")
    assert entry["Name"] == "Text Editor" and entry["Exec"] == "gedit --new-window %U"
    assert exec_argv(entry) == ["gedit", "--new-window"]
    assert find_desktop_entry("gedit", [apps]) == apps / "org.gnome.gedit.desktop"
    assert find_desktop_entry("text editor", [apps]) == apps / "org.gnome.gedit.desktop"
    assert find_desktop_entry("editor", [apps]) == apps / "org.gnome.gedit.desktop"  # a partial name is the last resort
    assert find_desktop_entry("hidden", [apps]) is None


@posix_only
def test_the_finder_stops_at_the_requested_stage(tmp_path, monkeypatch):
    opt = tmp_path / "opt" / "vendor"
    opt.mkdir(parents=True)
    exe = opt / "myapp"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share2"))
    from custom_console.platform import linux
    monkeypatch.setattr(linux.Path, "home", classmethod(lambda cls: tmp_path))
    desktop = for_system("linux").desktop
    assert desktop.find_app("myapp", level=2) is None
    on_path = tmp_path / "bin"
    on_path.mkdir()
    (on_path / "tool").write_text("x")
    (on_path / "tool").chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path))
    match = desktop.find_app("tool", level=1)
    assert match is not None and match.stage == 1 and match.target == str(on_path / "tool")


# ---------------------------------------------------------------------- process runner
def test_head_tail_keeps_both_ends_and_counts_what_it_dropped():
    buffer = HeadTail(keep=20)
    for i in range(10):
        buffer.write(f"line{i}\n".encode())
    text = buffer.text()
    assert text.startswith("line0") and text.rstrip().endswith("line9")
    assert "bytes left out" in text and buffer.dropped > 0 and buffer.total == 60
    small = HeadTail(keep=100)
    small.write(b"abc")
    assert small.text() == "abc" and small.dropped == 0


def test_decode_never_fails():
    assert decode("é".encode()) == "é"
    assert isinstance(decode(b"\xff\xfe\x00bad"), str)


def test_run_capture_collects_output_and_exit_code():
    done = run_capture([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], timeout=20)
    assert done.returncode == 3 and not done.ok
    assert "out" in done.output and "err" in done.output


def test_input_is_closed_so_a_question_gets_an_end_of_file():
    begun = time.monotonic()
    done = run_capture([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=20)
    assert done.output.strip() == "''" and time.monotonic() - begun < 10


def test_input_text_is_given_when_asked():
    done = run_capture([PY, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="abc", timeout=20)
    assert done.output.strip() == "ABC"


def test_timeout_stops_the_whole_process_tree(tmp_path):
    marker = tmp_path / "grandchild"
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', 'import time, pathlib; time.sleep(1.5); pathlib.Path(r\"{marker}\").write_text(\"alive\")'])\n"
        "time.sleep(60)\n")
    begun = time.monotonic()
    done = run_capture([PY, "-c", code], timeout=0.7)
    assert done.timed_out and not done.ok and time.monotonic() - begun < 10
    time.sleep(2.0)
    assert not marker.exists(), "the grandchild survived the timeout"


def test_cancel_stops_a_running_process():
    token = CancelToken()
    import threading
    threading.Timer(0.4, token.cancel).start()
    begun = time.monotonic()
    done = run_capture([PY, "-c", "import time; time.sleep(60)"], cancel=token, timeout=60)
    assert done.cancelled and time.monotonic() - begun < 10
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()
    assert token.wait(0)


def test_a_missing_program_is_a_readable_error():
    with pytest.raises(StartError, match="not found"):
        run_capture(["definitely-not-a-program-xyz"])
    with pytest.raises(StartError, match="command not found"):
        run_attached(["definitely-not-a-program-xyz"])


def test_large_output_is_bounded_in_memory():
    done = run_capture([PY, "-c", "print('x' * 5_000_000)"], keep=10_000, timeout=30)
    assert len(done.output) < 20_000 and "bytes left out" in done.output


def test_background_job_output_and_stop():
    job = start_background(f'"{PY}" -u -c "import time; print(1, flush=True); time.sleep(60)"')
    for _ in range(100):
        if "1" in job.output():
            break
        time.sleep(0.05)
    assert job.running and "1" in job.output()
    job.stop()
    assert job.wait(5) and not job.running and job.stopped
    finished = start_background(f'"{PY}" -c "print(42)"')
    assert finished.wait(10) and finished.returncode == 0 and "42" in finished.output()


def test_run_attached_returns_the_exit_code():
    assert run_attached([PY, "-c", "import sys; sys.exit(4)"]) == 4


class FakeWindowsTools:
    """`whoami` and `icacls` as the PC printed them: the SID in csv, the access list as UTF-16 SDDL written by /save."""

    def __init__(self, sddl: str, whoami_ok: bool = True):
        self.sddl, self.whoami_ok, self.calls = sddl, whoami_ok, []

    def __call__(self, args, **kwargs):
        import subprocess
        self.calls.append(list(args))
        if args[0] == "whoami":
            out = f'"erwan\\\\erwan","{ME}"\n' if self.whoami_ok else "ERROR: nope\n"
            return subprocess.CompletedProcess(args, 0 if self.whoami_ok else 1, out, "")
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(b"\xff\xfe" + f"token\n\n{self.sddl}\n".encode("utf-16-le"))
        return subprocess.CompletedProcess(args, 0, "Successfully processed 1 files; Failed processing 0 files\n", "")


def test_the_windows_check_reads_the_saved_list_and_names_the_stray_entry(monkeypatch, tmp_path):
    from custom_console.platform import windows
    privacy = windows.WindowsPrivacy()
    monkeypatch.setattr(windows.subprocess, "run", FakeWindowsTools(SDDL_REAL))
    problem = privacy.problem(tmp_path / "token")
    assert problem is not None and MACHINE in problem and privacy.sid() == ME
    monkeypatch.setattr(windows.subprocess, "run", FakeWindowsTools(SDDL_OK))
    assert privacy.problem(tmp_path / "token") is None


def test_the_windows_check_says_when_it_cannot_check(monkeypatch, tmp_path):
    from custom_console.platform import windows
    monkeypatch.setattr(windows.subprocess, "run", FakeWindowsTools(SDDL_OK, whoami_ok=False))
    assert "Cannot check the access list" in (windows.WindowsPrivacy().problem(tmp_path / "token") or "")


def test_a_private_file_is_granted_by_sid_never_by_name(monkeypatch, tmp_path):
    from custom_console.platform import windows
    tools = FakeWindowsTools(SDDL_OK)
    monkeypatch.setattr(windows.subprocess, "run", tools)
    target = tmp_path / "token"
    target.write_text("x")
    windows.WindowsPrivacy().make_private(target)
    grants = next(c for c in tools.calls if c[0] == "icacls")
    assert f"*{ME}:F" in grants and "*S-1-5-18:F" in grants and "/inheritance:r" in grants and "erwan:F" not in grants
