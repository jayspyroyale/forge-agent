"""Tools that modify files: write_file and edit_file.

Both resolve paths with `Workspace.ensure_writable` (inside the workspace, not
protected) and write atomically through `forge.fileio`.
"""

from pathlib import Path

from pydantic import Field

from forge.fileio import NotTextError, atomic_write_text, normalize_newlines, read_text_file, split_lines
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

MAX_REPORTED_MATCH_LINES = 10


class WriteFileArgs(ToolArgs):
    path: str = Field(description="File to write, relative to the workspace root.")
    content: str = Field(description="The complete new content of the file.")
    overwrite: bool = Field(
        default=False,
        description="Must be true to replace an existing file. Prefer edit_file for changes to existing files.",
    )
    create_dirs: bool = Field(default=True, description="Create missing parent directories.")


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new text file, or replace an existing file's entire content (requires overwrite=true). "
        "For changes to part of an existing file, use edit_file instead."
    )
    Args = WriteFileArgs

    def execute(self, args: WriteFileArgs, context: ToolContext) -> ToolResult:
        workspace = context.workspace
        path = workspace.ensure_writable(args.path)
        relative = workspace.relative(path)

        if path.is_dir():
            raise ToolError(f"{relative} is a directory")
        existed = path.exists()
        if existed and not args.overwrite:
            raise ToolError(
                f"{relative} already exists. Pass overwrite=true to replace it, "
                "or use edit_file to change part of it."
            )

        newline, bom = "\n", False
        if existed:
            try:
                original = read_text_file(path)
                newline, bom = original.newline, original.bom
            except NotTextError:
                pass  # replacing a non-text file: write plain UTF-8 with "\n"

        _prepare_parent(path, args.create_dirs, workspace.relative)
        written = atomic_write_text(path, args.content, newline=newline, bom=bom)
        line_count = len(split_lines(normalize_newlines(args.content)))
        action = "Overwrote" if existed else "Created"
        return ToolResult.ok(
            f"{action} {relative} ({line_count} lines, {written} bytes)",
            path=relative,
            created=not existed,
            bytes=written,
            lines=line_count,
            changed_paths=[relative],
        )


class EditFileArgs(ToolArgs):
    path: str = Field(description="File to edit, relative to the workspace root.")
    old_text: str = Field(
        min_length=1,
        description="Exact text to replace, including whitespace. Must match exactly one place unless replace_all is true.",
    )
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = Field(default=False, description="Replace every occurrence of old_text.")


class EditFile(Tool):
    name = "edit_file"
    description = (
        "Replace an exact piece of text in an existing file. old_text must match exactly once "
        "(copy it from read_file, including indentation), unless replace_all is true. "
        "Unrelated content is left untouched."
    )
    Args = EditFileArgs

    def execute(self, args: EditFileArgs, context: ToolContext) -> ToolResult:
        workspace = context.workspace
        path = workspace.ensure_writable(args.path)
        relative = workspace.relative(path)

        if not path.exists():
            raise ToolError(f"File not found: {relative}. Use write_file to create a new file.")
        if path.is_dir():
            raise ToolError(f"{relative} is a directory")
        try:
            original = read_text_file(path)
        except NotTextError as error:
            raise ToolError(str(error)) from None

        old_text = normalize_newlines(args.old_text)
        new_text = normalize_newlines(args.new_text)
        if old_text == new_text:
            raise ToolError("old_text and new_text are identical; nothing to change.")

        match_lines = _match_line_numbers(original.text, old_text)
        if not match_lines:
            raise ToolError(
                f"old_text was not found in {relative}. Read the file again and copy the exact text, "
                "including indentation and blank lines."
            )
        if len(match_lines) > 1 and not args.replace_all:
            shown = ", ".join(str(line) for line in match_lines[:MAX_REPORTED_MATCH_LINES])
            raise ToolError(
                f"old_text matches {len(match_lines)} places in {relative} (lines {shown}). "
                "Include more surrounding text so it matches exactly once, or pass replace_all=true."
            )

        updated = original.text.replace(old_text, new_text)
        atomic_write_text(path, updated, newline=original.newline, bom=original.bom)

        count = len(match_lines)
        before, after = len(split_lines(original.text)), len(split_lines(updated))
        plural = "occurrence" if count == 1 else "occurrences"
        return ToolResult.ok(
            f"Edited {relative}: replaced {count} {plural} (starting at line {match_lines[0]}); "
            f"file now has {after} lines.",
            path=relative,
            replacements=count,
            match_lines=match_lines,
            lines_before=before,
            lines_after=after,
            changed_paths=[relative],
        )


def _match_line_numbers(text: str, needle: str) -> list[int]:
    """1-based line numbers where each non-overlapping occurrence of needle starts."""
    lines = []
    start = 0
    while (index := text.find(needle, start)) != -1:
        lines.append(text.count("\n", 0, index) + 1)
        start = index + len(needle)
    return lines


def _prepare_parent(path: Path, create_dirs: bool, display) -> None:
    parent = path.parent
    if parent.is_dir():
        return
    if parent.exists():
        raise ToolError(f"Cannot create {display(path)}: {display(parent)} is a file, not a directory")
    if not create_dirs:
        raise ToolError(f"Directory {display(parent)} does not exist. Pass create_dirs=true to create it.")
    parent.mkdir(parents=True, exist_ok=True)
