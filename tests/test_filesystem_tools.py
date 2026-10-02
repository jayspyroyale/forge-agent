from helpers import run_tool

# --- The explicit workspace boundary case ------------------------------------


def test_read_allowed_file_succeeds(workspace_root):
    result = run_tool(workspace_root, "read_file", path="allowed.txt")
    assert result.success
    assert result.output == "hello from inside\n"


def test_read_outside_file_fails_safely(workspace_root):
    result = run_tool(workspace_root, "read_file", path="../outside.txt")
    assert not result.success
    assert "outside the workspace" in result.error
    assert "secret" not in result.to_model_content()


def test_read_absolute_path_outside_fails(workspace_root):
    outside = str((workspace_root.parent / "outside.txt").resolve())
    result = run_tool(workspace_root, "read_file", path=outside)
    assert not result.success
    assert "outside the workspace" in result.error


def test_every_path_tool_refuses_to_leave_the_workspace(workspace_root):
    for name, arguments in [
        ("read_file", {"path": "../outside.txt"}),
        ("list_files", {"path": ".."}),
        ("file_exists", {"path": "../outside.txt"}),
        ("search_text", {"pattern": "secret", "path": ".."}),
    ]:
        result = run_tool(workspace_root, name, **arguments)
        assert not result.success, name
        assert "outside the workspace" in result.error, name


# --- read_file ----------------------------------------------------------------


def test_read_missing_file(workspace_root):
    result = run_tool(workspace_root, "read_file", path="missing.py")
    assert not result.success
    assert result.error == "File not found: missing.py"


def test_read_directory_is_rejected(workspace_root):
    (workspace_root / "src").mkdir()
    result = run_tool(workspace_root, "read_file", path="src")
    assert not result.success
    assert "is a directory" in result.error


def test_read_binary_file_is_rejected(workspace_root):
    (workspace_root / "image.bin").write_bytes(b"\x89PNG\x00\x00data")
    result = run_tool(workspace_root, "read_file", path="image.bin")
    assert not result.success
    assert "binary" in result.error


def test_read_invalid_utf8_is_rejected(workspace_root):
    (workspace_root / "latin1.txt").write_bytes("café".encode("latin-1"))
    result = run_tool(workspace_root, "read_file", path="latin1.txt")
    assert not result.success
    assert "not valid UTF-8" in result.error


def test_read_unicode(workspace_root):
    text = "héllo wörld ✓\n"
    (workspace_root / "unicode.txt").write_text(text, encoding="utf-8")
    assert run_tool(workspace_root, "read_file", path="unicode.txt").output == text


def test_read_line_range(workspace_root):
    (workspace_root / "lines.txt").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    result = run_tool(workspace_root, "read_file", path="lines.txt", start_line=3, max_lines=2)
    assert result.success
    assert result.output.startswith("line 3\nline 4\n")
    assert "use start_line=5 to continue" in result.output
    assert result.metadata["total_lines"] == 10
    assert (result.metadata["start_line"], result.metadata["end_line"]) == (3, 4)


def test_read_start_line_past_end(workspace_root):
    result = run_tool(workspace_root, "read_file", path="allowed.txt", start_line=5)
    assert not result.success


def test_read_empty_file(workspace_root):
    (workspace_root / "empty.txt").write_text("")
    result = run_tool(workspace_root, "read_file", path="empty.txt")
    assert result.success
    assert result.output == ""


def test_read_large_output_is_truncated(workspace_root):
    (workspace_root / "big.txt").write_text(("y" * 99 + "\n") * 2000)
    result = run_tool(workspace_root, "read_file", path="big.txt")
    assert result.success
    assert result.metadata["truncated"] is True
    assert len(result.output) < 110_000
    assert "use start_line=" in result.output


# --- list_files ---------------------------------------------------------------


def test_list_files(workspace_root):
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.py").write_text("x = 1\n")
    (workspace_root / "__pycache__").mkdir()

    result = run_tool(workspace_root, "list_files")

    assert result.success
    assert result.output.splitlines() == ["allowed.txt", "src/"]


def test_list_files_recursive(workspace_root):
    (workspace_root / "src" / "pkg").mkdir(parents=True)
    (workspace_root / "src" / "pkg" / "mod.py").write_text("")
    result = run_tool(workspace_root, "list_files", recursive=True)
    assert result.output.splitlines() == ["allowed.txt", "src/pkg/mod.py"]


def test_list_files_limit(workspace_root):
    for n in range(5):
        (workspace_root / f"file{n}.txt").write_text("")
    result = run_tool(workspace_root, "list_files", max_entries=2)
    assert result.metadata["truncated"] is True
    assert "more entries not shown" in result.output


def test_list_files_on_a_file_fails(workspace_root):
    assert not run_tool(workspace_root, "list_files", path="allowed.txt").success


def test_list_missing_directory(workspace_root):
    assert not run_tool(workspace_root, "list_files", path="nope").success


# --- file_exists / current_directory ------------------------------------------


def test_file_exists(workspace_root):
    (workspace_root / "src").mkdir()
    assert run_tool(workspace_root, "file_exists", path="allowed.txt").metadata == {"exists": True, "type": "file"}
    assert run_tool(workspace_root, "file_exists", path="src").metadata["type"] == "directory"
    missing = run_tool(workspace_root, "file_exists", path="missing.txt")
    assert missing.success
    assert missing.metadata["exists"] is False


def test_current_directory(workspace_root):
    result = run_tool(workspace_root, "current_directory")
    assert result.output == str(workspace_root.resolve())


# --- search_text --------------------------------------------------------------


def test_search_text_finds_lines(workspace_root):
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.py").write_text("def main():\n    return greet()\n\ndef greet():\n    pass\n")

    result = run_tool(workspace_root, "search_text", pattern="greet")

    assert result.success
    assert result.output.splitlines() == [
        "src/app.py:2: return greet()",
        "src/app.py:4: def greet():",
    ]
    assert result.metadata["matches"] == 2


def test_search_text_is_literal_by_default(workspace_root):
    (workspace_root / "a.txt").write_text("cost is $5 (approx)\n")
    assert run_tool(workspace_root, "search_text", pattern="$5 (approx)").metadata["matches"] == 1


def test_search_text_regex_and_case(workspace_root):
    (workspace_root / "a.txt").write_text("Alpha\nbeta\n")
    assert run_tool(workspace_root, "search_text", pattern="^[ab]", regex=True).metadata["matches"] == 1
    insensitive = run_tool(workspace_root, "search_text", pattern="^[ab]", regex=True, case_sensitive=False)
    assert insensitive.metadata["matches"] == 2


def test_search_text_invalid_regex(workspace_root):
    result = run_tool(workspace_root, "search_text", pattern="(", regex=True)
    assert not result.success
    assert "Invalid regular expression" in result.error


def test_search_text_skips_binary_files(workspace_root):
    (workspace_root / "data.bin").write_bytes(b"needle\x00\x01")
    (workspace_root / "text.txt").write_text("needle\n")
    result = run_tool(workspace_root, "search_text", pattern="needle")
    assert result.output.splitlines() == ["text.txt:1: needle"]


def test_search_text_skips_ignored_directories(workspace_root):
    (workspace_root / ".venv").mkdir()
    (workspace_root / ".venv" / "lib.py").write_text("needle\n")
    assert run_tool(workspace_root, "search_text", pattern="needle").output == "No matches found."


def test_search_text_limits_results(workspace_root):
    (workspace_root / "many.txt").write_text("needle\n" * 20)
    result = run_tool(workspace_root, "search_text", pattern="needle", max_results=5)
    assert result.metadata["matches"] == 5
    assert result.metadata["truncated"] is True
    assert "stopped after 5 matches" in result.output


def test_search_text_in_single_file(workspace_root):
    result = run_tool(workspace_root, "search_text", pattern="hello", path="allowed.txt")
    assert result.output == "allowed.txt:1: hello from inside"


def test_read_crlf_file_is_shown_with_lf(workspace_root):
    (workspace_root / "windows.txt").write_bytes(b"first\r\nsecond\r\n")
    assert run_tool(workspace_root, "read_file", path="windows.txt").output == "first\nsecond\n"
