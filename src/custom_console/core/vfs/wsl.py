"""A WSL distribution, reached through its `\\\\wsl.localhost\\<distro>` share (an ordinary folder for Windows).

It behaves like the local disk; it is its own backend so a message can say where a failure happened, and so the
permission engine can tell it apart from other network paths.
"""

from __future__ import annotations

from .local import LocalBackend

HOST = "wsl.localhost"


def unc(distro: str, parts: tuple[str, ...] = ()) -> str:
    """`\\\\wsl.localhost\\Ubuntu\\home\\me` for the distribution `Ubuntu` and the parts `("home", "me")`."""
    return "\\\\" + "\\".join((HOST, distro, *parts))


class WslBackend(LocalBackend):
    name = "wsl"

    def __init__(self, distro: str):
        self.distro = distro
        self.label = f"WSL ({distro})"
