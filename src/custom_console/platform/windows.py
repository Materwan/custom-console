"""Windows: access lists through icacls, `os.startfile`, the registry's App Paths, PowerShell for the clipboard.

This module imports on every system (the Windows-only calls are looked up when they run), so its parsing is tested on
Linux. The calls themselves are checked on the Windows runner and by hand.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import getpass
import importlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import AppMatch, DesktopError, Platform
from .posix import look_in, walk_for

PNG = b"\x89PNG\r\n\x1a\n"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
ERROR_BAD_EXE_FORMAT = 193

# Access lists are judged by SID, never by name: names are translated (`Administrateurs`), and a computer called like its user
# makes `erwan` mean the computer for icacls. `icacls <path> /save <file>` writes the list as SDDL, which has only SIDs and
# the short aliases of the well-known ones.
SYSTEM_SID = "S-1-5-18"
ALLOWED_TRUSTEES = frozenset({
    "SY", SYSTEM_SID,  # the system
    "BA", "S-1-5-32-544",  # Administrators
    "CO", "S-1-3-0",  # Creator Owner
    "OW", "S-1-3-4",  # Owner Rights
})
ALIAS_SIDS = {"WD": "S-1-1-0", "BU": "S-1-5-32-545", "AU": "S-1-5-11", "IU": "S-1-5-4", "NU": "S-1-5-2", "AN": "S-1-5-7",
              "LS": "S-1-5-19", "NS": "S-1-5-20", "BG": "S-1-5-32-546", "PU": "S-1-5-32-547"}
TRUSTEE_NAMES = {"S-1-1-0": "Everyone", "S-1-5-32-545": "Users", "S-1-5-11": "Authenticated Users", "S-1-5-4": "Interactive users",
                 "S-1-5-2": "Network users", "S-1-5-7": "Anonymous", "S-1-5-32-546": "Guests", "S-1-5-32-547": "Power Users"}
TRUSTED_INSTALLER_PREFIX = "S-1-5-80-"
RIGHT_BITS = {"FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0, "GA": 0x10000000, "GR": 0x80000000,
              "GW": 0x40000000, "GX": 0x20000000}
READ_MASK = 0x1 | 0x10000000 | 0x80000000  # read data / list folder, generic all, generic read
ACE_SDDL = re.compile(r"\((?P<type>[A-Za-z]+);(?P<flags>[A-Z]*);(?P<rights>[A-Za-z0-9]*);(?P<object>[^;]*);(?P<inherit>[^;]*);"
                      r"(?P<who>[^;)]+)\)")


def rights_mask(rights: str) -> int:
    """The access mask of an SDDL rights field: `0x1200a9`, or two-letter codes such as `FA`, `FRFX`, `GR`."""
    if rights.lower().startswith("0x"):
        try:
            return int(rights, 16)
        except ValueError:
            return READ_MASK  # unreadable: assume the worst
    mask = 0
    for index in range(0, len(rights), 2):
        mask |= RIGHT_BITS.get(rights[index:index + 2].upper(), 0)
    return mask


def parse_sddl(sddl: str) -> list[tuple[str, int]]:
    """The `(trustee, access mask)` of every *allow* entry of an SDDL string. Entries that deny grant nothing."""
    entries = []
    for found in ACE_SDDL.finditer(sddl):
        if found.group("type").upper() in ("A", "OA"):
            entries.append((found.group("who"), rights_mask(found.group("rights"))))
    return entries


def acl_problem(path: Path, entries: list[tuple[str, int]], my_sid: str) -> str | None:
    """The first trustee other than you and the system accounts that can read `path`, as a sentence with the fix."""
    for who, mask in entries:
        if who.upper() in ALLOWED_TRUSTEES or who == my_sid or who.startswith(TRUSTED_INSTALLER_PREFIX):
            continue
        if mask & READ_MASK:
            sid = ALIAS_SIDS.get(who.upper(), who)
            name = TRUSTEE_NAMES.get(sid, sid)
            return (f"{path} can be read by {name}: run  icacls \"{path}\" /inheritance:r /grant:r \"*{my_sid}:F\" "
                    f"/remove \"*{sid}\"")
    return None


class WindowsPrivacy:
    tightens = False

    def __init__(self) -> None:
        self.user = getpass.getuser()
        self._sid = ""

    def _icacls(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=20, check=False,
                              stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def sid(self) -> str:
        """Your own SID, from `whoami /user` (the only exact way to say who you are)."""
        if not self._sid:
            done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=20,
                                  check=False, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            rows = list(csv.reader(done.stdout.strip().splitlines()))
            if done.returncode != 0 or not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
                raise OSError(f"Cannot find your SID: {done.stdout.strip() or done.stderr.strip() or 'whoami failed'}")
            self._sid = rows[0][1]
        return self._sid

    def problem(self, path: Path) -> str | None:
        try:
            sid = self.sid()
            with tempfile.TemporaryDirectory() as folder:
                saved = Path(folder) / "acl.txt"
                done = self._icacls(str(path), "/save", str(saved))
                if done.returncode != 0 or not saved.exists():
                    return f"Cannot check the access list of {path}: {done.stdout.strip() or done.stderr.strip()}"
                raw = saved.read_bytes()
        except (OSError, subprocess.SubprocessError) as error:
            return f"Cannot check the access list of {path}: {error}"
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:8] else raw.decode("utf-8", "replace")
        return acl_problem(path, parse_sddl(text), sid)

    def make_private(self, path: Path) -> None:
        """Only you and the system. Granted by SID: a computer named like its user makes `erwan:F` mean the computer."""
        grant = "(OI)(CI)F" if path.is_dir() else "F"
        try:
            me = f"*{self.sid()}"
        except OSError:
            me = self.user
        done = self._icacls(str(path), "/inheritance:r", "/grant:r", f"{me}:{grant}", "/grant:r", f"*{SYSTEM_SID}:{grant}")
        if done.returncode != 0:
            raise OSError(done.stdout.strip() or done.stderr.strip() or "icacls failed")

    def write_private(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.close(descriptor)
        try:
            self.make_private(Path(temporary))  # restricted while still empty
            Path(temporary).write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


class WindowsPaths:
    case_insensitive = True
    WSL_PREFIXES = ("\\\\wsl$\\", "\\\\wsl.localhost\\")

    @staticmethod
    def _backslashed(raw: str) -> str:
        return raw.replace("/", "\\")

    def home(self) -> Path:
        return Path.home()

    def is_wsl_path(self, raw: str) -> bool:
        return self._backslashed(raw).lower().startswith(self.WSL_PREFIXES)

    def is_network_path(self, raw: str) -> bool:
        text = self._backslashed(raw)
        if not text.startswith("\\\\") or self.is_wsl_path(raw):
            return False
        if text.startswith(("\\\\?\\", "\\\\.\\")):  # extended-length and device paths are local unless `UNC\`
            return text[4:].lower().startswith("unc\\")
        return True

    def drives(self) -> list[str]:
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return []
        return [chr(ord("A") + i) for i in range(26) if mask & (1 << i)]

    def wsl_distros(self) -> list[str]:
        if shutil.which("wsl.exe") is None:
            return []
        try:
            done = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=10, check=False,
                                  stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_wsl_list(done.stdout) if done.returncode == 0 else []

    def credential_dirs(self, home: Path) -> tuple[Path, ...]:
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return (
            *(home / n for n in (".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker")),
            roaming / "Microsoft" / "Credentials", roaming / "Microsoft" / "Protect", roaming / "Microsoft" / "Vault",
            local / "Microsoft" / "Credentials", local / "Microsoft" / "Vault",
            local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
            local / "BraveSoftware", local / "Vivaldi", roaming / "Mozilla" / "Firefox", roaming / "Opera Software",
            roaming / "gcloud",
        )


def parse_wsl_list(raw: bytes) -> list[str]:
    """`wsl.exe -l -q` prints UTF-16 (little endian) names, one per line, sometimes with a byte order mark."""
    text = raw.decode("utf-16-le", errors="replace").replace("\ufeff", "").replace("\x00", "")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _powershell() -> str:
    for name in ("powershell.exe", "pwsh.exe", "pwsh"):
        if found := shutil.which(name):
            return found
    raise DesktopError("PowerShell was not found, so the clipboard cannot be used.")


IMAGE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$i=[System.Windows.Forms.Clipboard]::GetImage(); if($i -eq $null){exit 3}; "
    "$m=New-Object System.IO.MemoryStream; $i.Save($m,[System.Drawing.Imaging.ImageFormat]::Png); "
    "[Console]::Out.Write([Convert]::ToBase64String($m.ToArray()))"
)


class WindowsDesktop:
    def restart_console(self, argv: list[str]) -> None:
        """A new console window with a fresh copy; the caller then leaves (a Windows process cannot replace itself)."""
        import sys

        try:
            subprocess.Popen([sys.executable, "-m", "custom_console", *argv], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        except OSError as error:
            raise DesktopError(f"Could not start a new console: {error.strerror or error}") from None

    def open_path(self, path: Path) -> None:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise DesktopError("Opening files needs Windows.")
        try:
            startfile(str(path))
        except OSError as error:
            raise DesktopError(f"{path}: {error.strerror or error}") from None

    # ------------------------------------------------------------------ finder
    @staticmethod
    def _install_folders() -> list[Path]:
        names = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA")
        return [Path(v) for n in names if (v := os.environ.get(n))]

    @staticmethod
    def _app_paths(name: str) -> Path | None:
        try:
            winreg = importlib.import_module("winreg")
        except ImportError:
            return None
        exe = name if name.lower().endswith(".exe") else f"{name}.exe"
        suffix = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{exe}"
        wow = f"SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{exe}"
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, suffix), (winreg.HKEY_LOCAL_MACHINE, wow),
                          (winreg.HKEY_CURRENT_USER, suffix)):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "")
            except OSError:
                continue
            path = Path(str(value).strip('"'))
            if path.is_file():
                return path
        return None

    def find_app(self, name: str, level: int = 4) -> AppMatch | None:
        exe = name if name.lower().endswith(".exe") else f"{name}.exe"
        if level >= 1 and (found := shutil.which(name) or shutil.which(exe)):
            return AppMatch(name, found, 1)
        if level >= 2 and (hit := self._app_paths(name)):
            return AppMatch(name, str(hit), 2)
        folders = self._install_folders()
        if level >= 3:
            for base in folders:
                if hit := look_in([base], exe, one_level_deeper=True):
                    return AppMatch(name, str(hit), 3)
                for inner in (p for p in _subdirs(base)):
                    if hit := look_in([inner], exe, one_level_deeper=True):
                        return AppMatch(name, str(hit), 3)
        if level >= 4 and (hit := walk_for(folders, exe, skip=frozenset({"node_modules", ".git", "__pycache__", "cache"}))):
            return AppMatch(name, str(hit), 4)
        return None

    def _start(self, target: str) -> None:
        try:
            subprocess.Popen([target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)
        except OSError as error:
            if getattr(error, "winerror", None) == ERROR_BAD_EXE_FORMAT:  # a shortcut or a document
                self.open_path(Path(target))
                return
            raise DesktopError(f"{target}: {error.strerror or error}") from None

    def start_app(self, match: AppMatch) -> None:
        self._start(match.target)

    def start_file(self, path: Path) -> None:
        self._start(str(path))

    # ------------------------------------------------------------------ clipboard
    def _run(self, script: str, *, input_text: str | None = None, timeout: float = 15.0) -> subprocess.CompletedProcess[bytes]:
        command = [_powershell(), "-NoProfile", "-NonInteractive", "-STA", "-Command",
                   "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                   "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;" + script]
        try:
            return subprocess.run(command, input=input_text.encode("utf-8") if input_text is not None else None,
                                  capture_output=True, timeout=timeout, check=False,
                                  stdin=None if input_text is not None else subprocess.DEVNULL,
                                  creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopError(f"The clipboard could not be used ({type(error).__name__}).") from None

    def clipboard_read(self) -> str:
        done = self._run("Get-Clipboard -Raw")
        return done.stdout.decode("utf-8", errors="replace").removesuffix("\r\n") if done.returncode == 0 else ""

    def clipboard_write(self, text: str) -> None:
        done = self._run("Set-Clipboard -Value ([Console]::In.ReadToEnd())", input_text=text)
        if done.returncode != 0:
            raise DesktopError("The clipboard could not be written.")

    def clipboard_image(self) -> bytes | None:
        done = self._run(IMAGE_SCRIPT, timeout=30.0)
        if done.returncode != 0 or not done.stdout.strip():
            return None
        try:
            data = base64.b64decode(done.stdout.strip(), validate=True)
        except ValueError:
            return None
        return data if data.startswith(PNG) else None


def _subdirs(base: Path) -> list[Path]:
    try:
        return [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []


def make() -> Platform:
    return Platform("windows", WindowsPrivacy(), WindowsDesktop(), WindowsPaths())
