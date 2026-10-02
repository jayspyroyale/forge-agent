import os

import pytest

from forge.fileio import atomic_write_text
from helpers import run_tool

# --- write_file ---------------------------------------------------------------


def test_create_file(workspace_root):
    result = run_tool(workspace_root, "write_file", path="new.py", content="print('hi')\n")

    assert result.success
    assert (workspace_root / "new.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert result.metadata["created"] is True
    assert result.metadata["lines"] == 1
    assert result.metadata["changed_paths"] == ["new.py"]
    assert result.output.startswith("Created new.py")


def test_create_file_in_new_directory(workspace_root):
    result = run_tool(workspace_root, "write_file", path="src/pkg/mod.py", content="x = 1\n")
    assert result.success
    assert (workspace_root / "src" / "pkg" / "mod.py").exists()


def test_create_dirs_can_be_disabled(workspace_root):
    result = run_tool(workspace_root, "write_file", path="nope/mod.py", content="", create_dirs=False)
    assert not result.success
    assert "does not exist" in result.error
    assert not (workspace_root / "nope").exists()


def test_existing_file_is_not_overwritten_by_default(workspace_root):
    result = run_tool(workspace_root, "write_file", path="allowed.txt", content="replaced\n")
    assert not result.success
    assert "already exists" in result.error
    assert (workspace_root / "allowed.txt").read_text() == "hello from inside\n"


def test_overwrite_file(workspace_root):
    result = run_tool(workspace_root, "write_file", path="allowed.txt", content="replaced\n", overwrite=True)
    assert result.success
    assert result.metadata["created"] is False
    assert (workspace_root / "allowed.txt").read_text() == "replaced\n"


def test_write_to_directory_fails(workspace_root):
    (workspace_root / "src").mkdir()
    assert not run_tool(workspace_root, "write_file", path="src", content="x", overwrite=True).success


def test_write_parent_is_a_file(workspace_root):
    result = run_tool(workspace_root, "write_file", path="allowed.txt/child.txt", content="x")
    assert not result.success


def test_write_outside_workspace_fails(workspace_root):
    result = run_tool(workspace_root, "write_file", path="../evil.txt", content="x")
    assert not result.success
    assert "outside the workspace" in result.error
    assert not (workspace_root.parent / "evil.txt").exists()


def test_write_absolute_path_outside_fails(workspace_root):
    target = workspace_root.parent / "evil.txt"
    result = run_tool(workspace_root, "write_file", path=str(target.resolve()), content="x")
    assert not result.success
    assert not target.exists()


def test_overwrite_outside_file_fails(workspace_root):
    result = run_tool(workspace_root, "write_file", path="../outside.txt", content="pwned", overwrite=True)
    assert not result.success
    assert (workspace_root.parent / "outside.txt").read_text() == "secret from outside\n"


def test_write_into_git_directory_is_blocked(workspace_root):
    (workspace_root / ".git").mkdir()
    result = run_tool(workspace_root, "write_file", path=".git/hooks/pre-commit", content="evil")
    assert not result.success
    assert "protected" in result.error


def test_write_into_task_records_is_blocked(workspace_root):
    result = run_tool(workspace_root, "write_file", path=".forge/tasks/x/evidence.json", content="{}")
    assert not result.success
    assert "protected" in result.error


def test_write_unicode(workspace_root):
    text = "名前 = 'Forge' # ✓ café\n"
    assert run_tool(workspace_root, "write_file", path="u.py", content=text).success
    assert (workspace_root / "u.py").read_text(encoding="utf-8") == text


def test_write_empty_file(workspace_root):
    result = run_tool(workspace_root, "write_file", path="empty.txt", content="")
    assert result.success
    assert (workspace_root / "empty.txt").read_bytes() == b""


def test_overwrite_keeps_crlf_style(workspace_root):
    (workspace_root / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    run_tool(workspace_root, "write_file", path="win.txt", content="three\nfour\n", overwrite=True)
    assert (workspace_root / "win.txt").read_bytes() == b"three\r\nfour\r\n"


# --- edit_file ----------------------------------------------------------------

SOURCE = """def add(a, b):
    return a + b


def multiply(a, b):
    return a + b
"""


@pytest.fixture
def calculator(workspace_root):
    path = workspace_root / "calculator.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def test_simple_edit_preserves_unrelated_content(workspace_root, calculator):
    result = run_tool(
        workspace_root,
        "edit_file",
        path="calculator.py",
        old_text="def multiply(a, b):\n    return a + b",
        new_text="def multiply(a, b):\n    return a * b",
    )

    assert result.success, result.error
    assert calculator.read_text() == SOURCE.replace(
        "def multiply(a, b):\n    return a + b", "def multiply(a, b):\n    return a * b"
    )
    assert "def add(a, b):\n    return a + b" in calculator.read_text()
    assert result.metadata["replacements"] == 1
    assert result.metadata["match_lines"] == [5]
    assert result.metadata["changed_paths"] == ["calculator.py"]


def test_old_text_not_found(workspace_root, calculator):
    result = run_tool(workspace_root, "edit_file", path="calculator.py", old_text="return a - b", new_text="x")
    assert not result.success
    assert "was not found" in result.error
    assert calculator.read_text() == SOURCE


def test_duplicate_match_fails_without_guessing(workspace_root, calculator):
    result = run_tool(workspace_root, "edit_file", path="calculator.py", old_text="return a + b", new_text="return 0")
    assert not result.success
    assert "matches 2 places" in result.error
    assert "lines 2, 6" in result.error
    assert calculator.read_text() == SOURCE


def test_replace_all(workspace_root, calculator):
    result = run_tool(
        workspace_root, "edit_file", path="calculator.py", old_text="a + b", new_text="b + a", replace_all=True
    )
    assert result.success
    assert result.metadata["replacements"] == 2
    assert "a + b" not in calculator.read_text()


def test_identical_text_is_rejected(workspace_root, calculator):
    result = run_tool(workspace_root, "edit_file", path="calculator.py", old_text="def add", new_text="def add")
    assert not result.success


def test_empty_old_text_is_rejected(workspace_root, calculator):
    result = run_tool(workspace_root, "edit_file", path="calculator.py", old_text="", new_text="x")
    assert not result.success
    assert "old_text" in result.error


def test_edit_missing_file(workspace_root):
    result = run_tool(workspace_root, "edit_file", path="missing.py", old_text="a", new_text="b")
    assert not result.success
    assert "File not found" in result.error


def test_edit_traversal_is_blocked(workspace_root):
    result = run_tool(workspace_root, "edit_file", path="../outside.txt", old_text="secret", new_text="public")
    assert not result.success
    assert (workspace_root.parent / "outside.txt").read_text() == "secret from outside\n"


def test_edit_unicode(workspace_root):
    (workspace_root / "u.txt").write_text("Grüße, 世界\n", encoding="utf-8")
    result = run_tool(workspace_root, "edit_file", path="u.txt", old_text="世界", new_text="Welt ✓")
    assert result.success
    assert (workspace_root / "u.txt").read_text(encoding="utf-8") == "Grüße, Welt ✓\n"


def test_edit_empty_file_finds_nothing(workspace_root):
    (workspace_root / "empty.txt").write_text("")
    result = run_tool(workspace_root, "edit_file", path="empty.txt", old_text="x", new_text="y")
    assert not result.success


def test_edit_crlf_file_with_lf_old_text(workspace_root):
    (workspace_root / "win.py").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
    result = run_tool(workspace_root, "edit_file", path="win.py", old_text="a = 1\nb = 2", new_text="a = 10\nb = 20")
    assert result.success
    assert (workspace_root / "win.py").read_bytes() == b"a = 10\r\nb = 20\r\nc = 3\r\n"


def test_edit_keeps_utf8_bom(workspace_root):
    (workspace_root / "bom.txt").write_bytes("﻿value = 1\n".encode("utf-8"))
    assert run_tool(workspace_root, "edit_file", path="bom.txt", old_text="1", new_text="2").success
    assert (workspace_root / "bom.txt").read_bytes() == "﻿value = 2\n".encode("utf-8")


def test_edit_binary_file_is_rejected(workspace_root):
    (workspace_root / "data.bin").write_bytes(b"abc\x00def")
    result = run_tool(workspace_root, "edit_file", path="data.bin", old_text="abc", new_text="xyz")
    assert not result.success
    assert (workspace_root / "data.bin").read_bytes() == b"abc\x00def"


# --- Atomicity ------------------------------------------------------------------


def test_failed_atomic_write_leaves_original_untouched(workspace_root, monkeypatch):
    target = workspace_root / "allowed.txt"

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        atomic_write_text(target, "new content\n")

    assert target.read_text() == "hello from inside\n"
    leftovers = [p.name for p in workspace_root.iterdir() if p.name.endswith(".forge-tmp")]
    assert leftovers == []


def test_tool_reports_write_failure_without_corrupting_file(workspace_root, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    result = run_tool(workspace_root, "write_file", path="allowed.txt", content="new\n", overwrite=True)
    assert not result.success
    assert "disk full" in result.error
    assert (workspace_root / "allowed.txt").read_text() == "hello from inside\n"
