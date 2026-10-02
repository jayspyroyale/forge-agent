"""Text file helpers shared by tools.

Models always see text with "\\n" line endings. When Forge writes a file back,
it restores the file's original line-ending style (and UTF-8 byte-order mark,
if it had one), so editing a Windows (CRLF) file does not silently rewrite
every line.

Writes are atomic: the new content goes to a temporary file in the same
directory, which then replaces the original in one step. If anything fails
midway, the original file is untouched.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

UTF8_BOM = "﻿"
BINARY_SNIFF_BYTES = 8192


class NotTextError(ValueError):
    """The file is binary or not valid UTF-8."""


@dataclass
class TextFile:
    text: str  # normalized to "\n" line endings, without a BOM
    newline: str  # the file's original line ending
    bom: bool  # whether the file started with a UTF-8 byte-order mark


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_newline(text: str) -> str:
    """The line ending a file uses: "\\r\\n" if its first line ending is CRLF, else "\\n"."""
    first = text.find("\n")
    if first > 0 and text[first - 1] == "\r":
        return "\r\n"
    return "\n"


def split_lines(text: str) -> list[str]:
    """Split normalized text into lines that keep their "\\n".

    Unlike str.splitlines(), only "\\n" counts as a line break, so line numbers
    match what editors show.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def read_text_file(path: Path) -> TextFile:
    data = path.read_bytes()
    if looks_binary(data):
        raise NotTextError(f"{path.name} looks like a binary file")
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotTextError(f"{path.name} is not valid UTF-8 text ({error.reason} at byte {error.start})") from None
    bom = raw.startswith(UTF8_BOM)
    if bom:
        raw = raw[1:]
    return TextFile(text=normalize_newlines(raw), newline=detect_newline(raw), bom=bom)


def atomic_write_text(path: Path, text: str, newline: str = "\n", bom: bool = False) -> int:
    """Write normalized `text` to `path` atomically. Returns the number of bytes written."""
    content = normalize_newlines(text)
    if newline != "\n":
        content = content.replace("\n", newline)
    return atomic_write_bytes(path, ((UTF8_BOM if bom else "") + content).encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> int:
    """Write `data` to `path` atomically: temp file in the same directory, then os.replace."""
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".forge-tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            # Keep the original file's permission bits (e.g. executable scripts).
            os.chmod(temp_path, path.stat().st_mode)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return len(data)
