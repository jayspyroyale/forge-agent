"""Structural summaries of large tool output.

When old output has to make room, Forge does not cut it at a random
character. It keeps what matters for that kind of output:

    test runs      the counts, and which tests failed
    commands       exit code, error lines, and the last lines
    file reads     the path, its size, and the first lines
    searches       how many matches, in which files
    anything else  the first and last lines

The full output is never lost: it stays in the task's tool history (and in
the saved task record). Only the copy sent to the model shrinks.
"""

import re
from collections import Counter
from typing import Any

HEADER = "[Forge: summary of earlier output; the full output is kept in the task record]"

_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_PYTEST_TOTALS = re.compile(r"=+ (.*?(?:passed|failed|error|errors|skipped)[^=]*?) in [\d.]+s", re.MULTILINE)
_PYTEST_SHORT_TOTALS = re.compile(r"^(\d+ (?:failed|passed|error|errors)(?:, \d+ \w+)*) in [\d.]+s", re.MULTILINE)
_ERROR_LINE = re.compile(r"(error|exception|traceback|failed|fatal|panic)", re.IGNORECASE)
_SEARCH_LINE = re.compile(r"^(.+?):\d+:", re.MULTILINE)

MAX_FAILURES = 15
MAX_ERROR_LINES = 10
TAIL_LINES = 5
HEAD_LINES = 10


def summarize_output(tool_name: str, arguments: dict[str, Any], content: str) -> str:
    """A short, structured stand-in for `content`, the text a tool returned to the model."""
    if tool_name == "run_command" or tool_name == "verification":
        body = _summarize_command(content)
    elif tool_name == "read_file":
        body = _summarize_file(str(arguments.get("path", "?")), content)
    elif tool_name == "search_text":
        body = _summarize_search(str(arguments.get("pattern", "?")), content)
    elif tool_name == "list_files":
        body = _summarize_listing(content)
    else:
        body = _head_tail(content)
    return f"{HEADER}\n{tool_name}: {body}"


def summarize_test_output(text: str) -> str | None:
    """`pytest: 3 failed, 10 passed` plus the failing test ids, if `text` looks like pytest output."""
    totals = _PYTEST_TOTALS.search(text) or _PYTEST_SHORT_TOTALS.search(text)
    failures = list(dict.fromkeys(_PYTEST_FAILED.findall(text)))
    if not totals and not failures:
        return None
    lines = [f"pytest: {totals.group(1).strip() if totals else f'{len(failures)} failed'}"]
    if failures:
        lines.append(f"{len(failures)} failure(s):")
        lines += [f"- {name}" for name in failures[:MAX_FAILURES]]
        if len(failures) > MAX_FAILURES:
            lines.append(f"- ... and {len(failures) - MAX_FAILURES} more")
    return "\n".join(lines)


def _summarize_command(content: str) -> str:
    lines = content.splitlines()
    header = [line for line in lines[:2] if line.startswith("$ ") or line.startswith("[")]
    parts = ["\n".join(header) or "(command output)"]
    tests = summarize_test_output(content)
    if tests:
        parts.append(tests)
    else:
        errors = [line.strip() for line in lines if _ERROR_LINE.search(line)]
        if errors:
            unique = list(dict.fromkeys(errors))[:MAX_ERROR_LINES]
            parts.append("error lines:\n" + "\n".join(f"  {line[:200]}" for line in unique))
    tail = [line for line in lines[2:] if line.strip() and not line.startswith("--- ")][-TAIL_LINES:]
    if tail:
        parts.append("last lines:\n" + "\n".join(f"  {line[:200]}" for line in tail))
    parts.append(f"({len(lines)} lines in full output)")
    return "\n".join(parts)


def _summarize_file(path: str, content: str) -> str:
    lines = content.splitlines()
    head = "\n".join(f"  {line[:160]}" for line in lines[:HEAD_LINES])
    return f"{path}, {len(lines)} lines. First lines:\n{head}\nRead the file again if you need its current content."


def _summarize_search(pattern: str, content: str) -> str:
    files = Counter(_SEARCH_LINE.findall(content))
    if not files:
        return f"pattern {pattern!r}: " + _head_tail(content)
    listed = ", ".join(f"{path} ({count})" for path, count in sorted(files.items())[:20])
    more = f", and {len(files) - 20} more files" if len(files) > 20 else ""
    return f"pattern {pattern!r}: {sum(files.values())} matches in {len(files)} files: {listed}{more}"


def _summarize_listing(content: str) -> str:
    entries = [line for line in content.splitlines() if line.strip()]
    shown = ", ".join(entries[:20])
    more = f", ... ({len(entries) - 20} more)" if len(entries) > 20 else ""
    return f"{len(entries)} entries: {shown}{more}"


def _head_tail(content: str) -> str:
    lines = content.splitlines()
    if len(lines) <= HEAD_LINES + TAIL_LINES:
        return content
    hidden = len(lines) - HEAD_LINES - TAIL_LINES
    return "\n".join([*lines[:HEAD_LINES], f"[... {hidden} lines omitted ...]", *lines[-TAIL_LINES:]])
