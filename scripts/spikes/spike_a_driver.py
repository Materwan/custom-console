"""Feed keys to spike_a.py through a pseudo-terminal and print what the terminal received."""

from __future__ import annotations

import os
import pathlib
import pty
import select
import sys
import time

KEYS = [b"he", b"\t", b"\x15", b"ls /C", b"\x1b[C", b"\r",   # menu, ghost text accepted with Right arrow
        b"ai agent\r", b"q",                                   # full-screen Textual, then back
        b"exit\r"]

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(__file__), "spike_a.py")])

out = b""
def drain(seconds: float) -> None:
    global out
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            out += chunk

drain(1.5)
for key in KEYS:
    os.write(fd, key)
    drain(1.2)
drain(1.0)
text = out.decode("utf-8", "replace")
for marker in ["help", "history", "hello", "LINE='ls /C:/Users'", "LINE='ai agent'", "AGENT-SCREEN", "AGENT-RESULT='agent-done'", "LINE='exit'", "SPIKE-A-OK"]:
    print(f"{'ok  ' if marker in text else 'MISS'} {marker}")
pathlib.Path(os.environ.get("SPIKE_DUMP", os.devnull)).write_text(text)
