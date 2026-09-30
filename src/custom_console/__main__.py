"""Entry point: ``python -m custom_console`` or the ``custom_console`` script."""

from __future__ import annotations

from .settings import get_settings
from .shell import Shell


def main() -> None:
    shell = Shell(get_settings())
    shell.run()
    shell.printer.console.clear()


if __name__ == "__main__":
    main()
