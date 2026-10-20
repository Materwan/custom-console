"""`@file` mentions and pasted images, turned into what goes to Clara.

* a small text file goes into the message as a fenced block with its path;
* a PDF, Word, spreadsheet, slide or image file is sent as a real attachment (base64) and Clara extracts its content;
* a folder goes in as the list of its names;
* a file over 30 MB is refused before anything is read.

This module reads files but never decides whether it may: the caller asks the permission engine for each mention first
(an `@` on a sensitive path asks like any other read).
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..clara.types import MAX_ATTACHMENT_BYTES, Attachment
from .vfs.backend import VfsError
from .vfs.ops import BinaryFile, Vfs
from .vfs.resolver import Resolved

INLINE_LIMIT = 100_000  # bytes of a text file put in the message; the model can read the rest with local_read
MAX_MENTIONS = 12
MAX_FOLDER_NAMES = 200
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ATTACH_SUFFIXES = frozenset({
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic",
})
TRAILING = ".,;:!?)]}>\"'"

# `@path`, `@"a path with spaces"` or `@'...'`; not inside a word (an e-mail address) and not `@@`
MENTION = re.compile(r"""(?<![\w@.])@(?:"([^"\n]+)"|'([^'\n]+)'|([^\s@]\S*))""")


@dataclass(frozen=True, slots=True)
class Mention:
    start: int
    end: int
    typed: str


@dataclass(slots=True)
class Mentioned:
    """A mention that points at something that exists."""

    typed: str
    resolved: Resolved
    kind: str  # "text", "attach" or "dir"
    size: int = 0


@dataclass(slots=True)
class Loaded:
    block: str = ""  # goes after the message
    attachment: Attachment | None = None
    note: str = ""  # one line for the person: what was attached or cut


@dataclass(slots=True)
class Prepared:
    message: str
    attachments: tuple[Attachment, ...] = ()
    notes: list[str] = field(default_factory=list)


def find_mentions(text: str) -> list[Mention]:
    found = []
    for match in MENTION.finditer(text):
        typed = match.group(1) or match.group(2) or match.group(3) or ""
        found.append(Mention(match.start(), match.end(), typed))
    return found[:MAX_MENTIONS]


def looks_like_a_path(typed: str) -> bool:
    return any(c in typed for c in "/\\~") or "." in typed.strip(".") or typed.startswith(".")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def suffix_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def resolve_mentions(text: str, vfs: Vfs) -> tuple[list[Mentioned], list[str]]:
    """What the `@` words of a message point at, and the problems that stop the message from being sent."""
    mentioned: list[Mentioned] = []
    problems: list[str] = []
    seen: set[str] = set()
    for mention in find_mentions(text):
        candidates = [mention.typed]
        stripped = mention.typed
        while stripped and stripped[-1] in TRAILING and len(candidates) < 4:  # "see @a.py." -> a.py
            stripped = stripped[:-1]
            candidates.append(stripped)
        chosen: tuple[str, Resolved, int, bool] | None = None
        error = ""
        for typed in candidates:
            if not typed:
                continue
            try:
                resolved = vfs.resolve(typed)
                info = vfs.stat(resolved)
            except VfsError as exc:
                error = exc.message
                continue
            if info is not None:
                chosen = (typed, resolved, info.size, info.is_dir)
                break
        if chosen is None:
            if looks_like_a_path(mention.typed):
                problems.append(f"@{mention.typed}: {error or 'no such file or folder'}")
            continue  # "@Erwan" is just a word
        typed, resolved, size, is_dir = chosen
        if resolved.virtual in seen:
            continue
        seen.add(resolved.virtual)
        if is_dir:
            kind = "dir"
        elif suffix_of(resolved.virtual) in ATTACH_SUFFIXES:
            kind = "attach"
        else:
            kind = "text"
        if kind != "dir" and size > MAX_ATTACHMENT_BYTES:
            problems.append(f"@{typed}: {human_size(size)} is more than the {MAX_ATTACHMENT_BYTES // 1_000_000} MB a file may be")
            continue
        mentioned.append(Mentioned(typed, resolved, kind, size))
    return mentioned, problems


def fence(text: str) -> str:
    """A fence longer than any run of backticks in the text, so the content cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def unique_name(name: str, taken: set[str]) -> str:
    candidate, number = name, 1
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    while candidate in taken:
        number += 1
        candidate = f"{stem} ({number}){'.' + extension if extension else ''}"
    taken.add(candidate)
    return candidate


def load(vfs: Vfs, item: Mentioned, taken_names: set[str]) -> Loaded:
    """Read one mentioned thing. A failure becomes a note, never an exception: the rest of the message still goes."""
    shown = item.resolved.virtual
    try:
        if item.kind == "dir":
            entries = vfs.list(item.resolved)
            names = [e.name + ("/" if e.is_dir else "") for e in sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))]
            more = len(names) - MAX_FOLDER_NAMES
            body = "\n".join(names[:MAX_FOLDER_NAMES]) + (f"\n[... {more} more ...]" if more > 0 else "")
            ticks = fence(body)
            return Loaded(f"[@{item.typed}: folder {shown}]\n{ticks}\n{body}\n{ticks}", None,
                          f"folder {shown}: {len(names)} entries")
        if item.kind == "attach":
            data = vfs.read_bytes(item.resolved, MAX_ATTACHMENT_BYTES + 1)
            if len(data) > MAX_ATTACHMENT_BYTES:
                return Loaded(note=f"{shown} is too large to attach")
            name = unique_name(shown.rstrip("/").rsplit("/", 1)[-1], taken_names)
            return Loaded(f"[@{item.typed}: attached as {name}]", Attachment(name, base64.b64encode(data).decode("ascii")),
                          f"attached {name} ({human_size(len(data))})")
        found = vfs.read_text(item.resolved, INLINE_LIMIT)
    except BinaryFile:
        return Loaded(note=f"{shown} is a binary file: only PDF, Word, spreadsheet, slide and image files can be attached")
    except VfsError as error:
        return Loaded(note=f"{shown}: {error.message}")
    text = found.text.rstrip("\n")
    ticks = fence(text)
    cut = f"\n[... cut at {INLINE_LIMIT:,} bytes of {found.size:,}; read the rest with local_read ...]" if found.truncated else ""
    return Loaded(f"[@{item.typed}: {shown}]\n{ticks}\n{text}{cut}\n{ticks}", None,
                  f"{shown}: {human_size(found.size)}" + (" (cut)" if found.truncated else ""))


def compose(text: str, loaded: Sequence[Loaded], extra: Sequence[Attachment] = ()) -> Prepared:
    blocks = [item.block for item in loaded if item.block]
    message = text if not blocks else text.rstrip() + "\n\n" + "\n\n".join(blocks)
    attachments = tuple([*extra, *(item.attachment for item in loaded if item.attachment is not None)])
    return Prepared(message, attachments, [item.note for item in loaded if item.note])


def pasted_image(data: bytes, number: int) -> Attachment:
    """A clipboard image as an attachment. Raises ValueError with a sentence for the person when it cannot be one."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("The clipboard image is not a PNG.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"The clipboard image is {human_size(len(data))}; the limit is {MAX_ATTACHMENT_BYTES // 1_000_000} MB.")
    return Attachment(f"pasted-image-{number}.png", base64.b64encode(data).decode("ascii"))
