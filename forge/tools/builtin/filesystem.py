"""Read-only filesystem tools. All paths go through `Workspace`."""

import re
from pathlib import Path

from pydantic import Field

from forge.fileio import looks_binary, normalize_newlines, split_lines
from forge.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

MAX_READ_FILE_BYTES = 10_000_000
MAX_READ_OUTPUT_CHARS = 100_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_SEARCH_LINE_CHARS = 200


class CurrentDirectory(Tool):
    name = "current_directory"
    description = "Return the absolute path of the workspace root. All relative paths are resolved from here."
    Args = NoArgs

    def execute(self, args: NoArgs, context: ToolContext) -> ToolResult:
        return ToolResult.ok(str(context.workspace.root))


class ListFilesArgs(ToolArgs):
    path: str = Field(default=".", description="Directory to list, relative to the workspace root.")
    recursive: bool = Field(default=False, description="List subdirectories recursively.")
    max_entries: int = Field(default=200, ge=1, le=1000, description="Maximum number of entries to return.")


class ListFiles(Tool):
    name = "list_files"
    description = (
        "List files and directories in the workspace. Directories end with '/'. "
        "Common noise directories (.git, .venv, node_modules, caches) are skipped."
    )
    Args = ListFilesArgs

    def execute(self, args: ListFilesArgs, context: ToolContext) -> ToolResult:
        workspace = context.workspace
        directory = workspace.resolve(args.path)
        if not directory.exists():
            raise ToolError(f"Directory not found: {args.path}")
        if not directory.is_dir():
            raise ToolError(f"Not a directory: {args.path}. Use read_file to read files.")

        if args.recursive:
            entries = [workspace.relative(path) for path in workspace.walk_files(directory)]
        else:
            entries = []
            for child in sorted(directory.iterdir(), key=lambda p: p.name):
                if workspace.is_ignored(child):
                    continue
                suffix = "/" if child.is_dir() else ""
                entries.append(workspace.relative(child) + suffix)

        truncated = len(entries) > args.max_entries
        shown = entries[: args.max_entries]
        output = "\n".join(shown) if shown else "(empty directory)"
        if truncated:
            output += f"\n[... {len(entries) - args.max_entries} more entries not shown]"
        return ToolResult.ok(output, count=len(shown), total=len(entries), truncated=truncated)


class ReadFileArgs(ToolArgs):
    path: str = Field(description="File to read, relative to the workspace root.")
    start_line: int = Field(default=1, ge=1, description="First line to return (1-based).")
    max_lines: int | None = Field(default=None, ge=1, description="Maximum number of lines to return.")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace. For long files, use start_line and "
        "max_lines to read a section."
    )
    Args = ReadFileArgs

    def execute(self, args: ReadFileArgs, context: ToolContext) -> ToolResult:
        path = context.workspace.resolve(args.path)
        if not path.exists():
            raise ToolError(f"File not found: {args.path}")
        if path.is_dir():
            raise ToolError(f"{args.path} is a directory. Use list_files to see its contents.")
        size = path.stat().st_size
        if size > MAX_READ_FILE_BYTES:
            raise ToolError(f"{args.path} is too large to read ({size} bytes). Use search_text instead.")

        data = path.read_bytes()
        if looks_binary(data):
            raise ToolError(f"{args.path} looks like a binary file and cannot be read as text.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolError(f"{args.path} is not valid UTF-8 text ({error.reason} at byte {error.start}).") from None

        lines = split_lines(normalize_newlines(text))
        total_lines = len(lines)
        start = args.start_line - 1
        if total_lines and start >= total_lines:
            raise ToolError(f"start_line {args.start_line} is past the end of the file ({total_lines} lines).")
        end = total_lines if args.max_lines is None else min(total_lines, start + args.max_lines)

        selected: list[str] = []
        size_so_far = 0
        for line in lines[start:end]:
            if size_so_far + len(line) > MAX_READ_OUTPUT_CHARS and selected:
                break
            selected.append(line)
            size_so_far += len(line)
        last_line = start + len(selected)

        output = "".join(selected)
        truncated = last_line < total_lines or start > 0
        if last_line < total_lines:
            output += (
                f"\n[showing lines {start + 1}-{last_line} of {total_lines}; "
                f"use start_line={last_line + 1} to continue]"
            )
        return ToolResult.ok(
            output,
            path=context.workspace.relative(path),
            total_lines=total_lines,
            start_line=start + 1,
            end_line=last_line,
            truncated=truncated,
        )


class FileExistsArgs(ToolArgs):
    path: str = Field(description="Path to check, relative to the workspace root.")


class FileExists(Tool):
    name = "file_exists"
    description = "Check whether a file or directory exists in the workspace."
    Args = FileExistsArgs

    def execute(self, args: FileExistsArgs, context: ToolContext) -> ToolResult:
        path = context.workspace.resolve(args.path)
        if path.is_dir():
            kind = "directory"
        elif path.exists():
            kind = "file"
        else:
            kind = None
        output = f"{args.path}: {kind}" if kind else f"{args.path}: does not exist"
        return ToolResult.ok(output, exists=kind is not None, type=kind)


class SearchTextArgs(ToolArgs):
    pattern: str = Field(min_length=1, description="Text to search for (or a regular expression if regex is true).")
    path: str = Field(default=".", description="File or directory to search in.")
    regex: bool = Field(default=False, description="Treat pattern as a Python regular expression.")
    case_sensitive: bool = Field(default=True, description="Match letter case exactly.")
    max_results: int = Field(default=50, ge=1, le=500, description="Maximum number of matching lines to return.")


class SearchText(Tool):
    name = "search_text"
    description = (
        "Search text files in the workspace. Returns matching lines as 'path:line: text'. "
        "Binary files, very large files, and noise directories are skipped."
    )
    Args = SearchTextArgs

    def execute(self, args: SearchTextArgs, context: ToolContext) -> ToolResult:
        workspace = context.workspace
        start = workspace.resolve(args.path)
        if not start.exists():
            raise ToolError(f"Path not found: {args.path}")

        flags = 0 if args.case_sensitive else re.IGNORECASE
        source = args.pattern if args.regex else re.escape(args.pattern)
        try:
            matcher = re.compile(source, flags)
        except re.error as error:
            raise ToolError(f"Invalid regular expression: {error}") from None

        matches: list[str] = []
        files_scanned = 0
        limit_reached = False
        for file_path in workspace.walk_files(start):
            text = _read_searchable_text(file_path)
            if text is None:
                continue
            files_scanned += 1
            relative = workspace.relative(file_path)
            for number, line in enumerate(split_lines(text), start=1):
                if matcher.search(line):
                    if len(matches) == args.max_results:
                        limit_reached = True
                        break
                    snippet = line.strip()
                    if len(snippet) > MAX_SEARCH_LINE_CHARS:
                        snippet = snippet[:MAX_SEARCH_LINE_CHARS] + "..."
                    matches.append(f"{relative}:{number}: {snippet}")
            if limit_reached:
                break

        if not matches:
            return ToolResult.ok("No matches found.", matches=0, files_scanned=files_scanned, truncated=False)
        output = "\n".join(matches)
        if limit_reached:
            output += f"\n[stopped after {args.max_results} matches; narrow the search to see more]"
        return ToolResult.ok(output, matches=len(matches), files_scanned=files_scanned, truncated=limit_reached)


def _read_searchable_text(path: Path) -> str | None:
    """File contents as text, or None for files search should skip."""
    try:
        if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if looks_binary(data):
        return None
    try:
        return normalize_newlines(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None
