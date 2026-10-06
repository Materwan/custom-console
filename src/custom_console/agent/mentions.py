"""``@path`` in a message: the file (or folder) is attached to it.

Typing ``@`` completes paths of the agent's working directory. When the message is sent, each
mentioned path that exists is read and given to the model with the message (a folder: its list of
entries), within a budget of characters; a file attached whole counts as read, so the agent may edit
it without reading it again. A mention that is not a path (``@someone``) is left alone.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Iterator, List, Optional

from prompt_toolkit.completion import Completer, Completion

from ..fs import BinaryFileError, FileManager
from .tools.state import ReadTracker

# "@path" or "@\"path with spaces\"", at the start or after a space (not inside an e-mail address)
MENTION = re.compile(r'(?<!\S)@(?:"([^"]+)"|([^\s"]+))')
TRAILING = ".,;:!?)]}'"
MIN_BLOCK_CHARS = 500  # a file that would get less than this of the budget is only named


def mentioned(text: str) -> List[str]:
    """The paths mentioned in `text`, in order, without duplicates."""
    found: List[str] = []
    for match in MENTION.finditer(text):
        path = match.group(1) or match.group(2).rstrip(TRAILING)
        if path and path not in found:
            found.append(path)
    return found


def attach(text: str, files: FileManager, reads: Optional[ReadTracker], budget: int) -> List[str]:
    """The blocks given to the model for the paths `text` mentions (see the module docstring)."""
    blocks: List[str] = []
    left = budget
    for raw in mentioned(text):
        try:
            local = files.local_path(raw, "attach")
        except Exception:
            continue
        if os.path.isdir(local):
            entries = files.listdir(raw)
            listing = "\n".join(entries[:200]) + (f"\n… and {len(entries) - 200} more" if len(entries) > 200 else "")
            block = f"[Folder mentioned by the user: {local}]\n{listing}"
        elif os.path.isfile(local):
            if left < MIN_BLOCK_CHARS:
                blocks.append(f"[File mentioned by the user, not attached (too much attached already): {local}]")
                continue
            try:
                read = files.read(raw, max_bytes=left)
            except BinaryFileError:
                blocks.append(f"[File mentioned by the user (binary, not attached): {local}]")
                continue
            except OSError:
                continue
            body = read.text[:left]
            whole = not read.truncated and len(body) == len(read.text)
            if reads is not None:
                if whole:
                    reads.mark(local)
                else:
                    reads.saw_part(local)
            cut = "" if whole else "\n[... cut: read the rest with file_system_read ...]"
            block = f"[File attached by the user: {local}]\n```\n{body}\n```{cut}"
        else:
            continue
        blocks.append(block)
        left -= len(block)
    return blocks


class MentionCompleter(Completer):
    """Completes the ``@path`` being typed, with the paths of the working directory."""

    def __init__(self, suggest: Callable[[str], list]):
        self.suggest = suggest

    def get_completions(self, document, complete_event) -> Iterator[Completion]:
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@") or word.startswith('@"'):
            return
        partial = word[1:]
        for text, is_dir in self.suggest(partial):
            shown = text + ("/" if is_dir else "")
            insert = f'@"{shown}"' if " " in shown else f"@{shown}"
            yield Completion(insert, start_position=-len(word), display=shown)
