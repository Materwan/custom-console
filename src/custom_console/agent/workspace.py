"""Sandboxed folders where the agent may freely read and write files.

Every path handed to the agent tools goes through :meth:`Workspace.resolve`,
which refuses anything that ends up outside the workspace root once ``..`` and
symbolic links are followed. The class works on the real file system directly
(not through the FileManager), so it is unaffected by where the user
navigated to.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePath
from typing import List, Mapping, Tuple


class Workspace:
    def __init__(self, roots: Mapping[str, Path]):
        self._roots = {name: Path(path) for name, path in roots.items()}

    @property
    def names(self) -> List[str]:
        return sorted(self._roots)

    def root(self, directory: str) -> Path:
        if directory not in self._roots:
            raise ValueError(
                f"Unknown workspace directory {directory!r}; expected one of {self.names}."
            )
        root = self._roots[directory].resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def resolve(self, directory: str, relative_path: str = ".") -> Path:
        """Absolute path of `relative_path` inside the `directory` workspace.

        Raises ValueError when the directory is unknown or the path escapes it
        (``..``, absolute paths, symlinks pointing outside).
        """
        root = self.root(directory)
        # Absolute, drive-qualified and UNC paths are refused before any file
        # system access: resolving a UNC path would probe the network.
        pure = PurePath(relative_path)
        if pure.is_absolute() or pure.drive or pure.root:
            raise ValueError(f"{relative_path!r} escapes the {directory!r} workspace directory.")
        candidate = (root / relative_path).resolve()
        if candidate != root and not candidate.is_relative_to(root):
            raise ValueError(f"{relative_path!r} escapes the {directory!r} workspace directory.")
        return candidate

    def describe(self, path: Path) -> str:
        """``result/notes/a.txt`` style name of an absolute workspace path."""
        for name in self.names:
            root = self.root(name)
            if path == root or path.is_relative_to(root):
                return "/".join((name, *path.relative_to(root).parts))
        return str(path)

    # -- operations --------------------------------------------------------- #

    def list(self, directory: str, path: str = ".") -> List[str]:
        target = self.resolve(directory, path)
        if not target.is_dir():
            raise NotADirectoryError(f"{path} is not a directory.")
        return sorted(
            (entry.name + "/" if entry.is_dir() else entry.name for entry in target.iterdir()),
            key=str.lower,
        )

    def read(
        self, directory: str, path: str, encoding: str = "utf-8", max_chars: int | None = None
    ) -> Tuple[str, bool]:
        """Text of a file and whether it was truncated to `max_chars`."""
        target = self.resolve(directory, path)
        if target.is_dir():
            raise IsADirectoryError(f"{path} is a directory.")
        if not target.is_file():
            raise FileNotFoundError(f"{path} is not a file.")
        text = target.read_text(encoding=encoding)
        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    def write(
        self,
        directory: str,
        path: str,
        content: str,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> Path:
        target = self.resolve(directory, path)
        if target.is_dir():
            raise IsADirectoryError(f"{path} is a directory.")
        if not overwrite and target.exists():
            raise FileExistsError(f"{path} already exists.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return target

    def delete(self, directory: str, path: str) -> Path:
        target = self.resolve(directory, path)
        if target == self.root(directory):
            raise ValueError("Refusing to delete the workspace root itself.")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise FileNotFoundError(f"{path} does not exist.")
        return target

    def move(self, directory: str, path: str, dst_directory: str, dst_path: str) -> Path:
        source = self.resolve(directory, path)
        destination = self.resolve(dst_directory, dst_path)
        if source == self.root(directory):
            raise ValueError("Refusing to move the workspace root itself.")
        if not source.exists():
            raise FileNotFoundError(f"{path} does not exist.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(source), os.fspath(destination))
        return destination
