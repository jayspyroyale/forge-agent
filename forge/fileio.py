"""Text file helpers shared by tools.

Models always see text with "\\n" line endings. When Forge writes a file back,
it restores the file's original line-ending style, so editing a Windows
(CRLF) file does not silently rewrite every line.
"""

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
