import os

import pytest

from forge.workspace import Workspace, WorkspaceError


def test_resolves_relative_paths_inside(workspace_root):
    workspace = Workspace(workspace_root)
    assert workspace.resolve("allowed.txt") == (workspace_root / "allowed.txt").resolve()
    assert workspace.relative("allowed.txt") == "allowed.txt"
    assert workspace.relative(".") == "."


def test_rejects_parent_traversal(workspace_root):
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        Workspace(workspace_root).resolve("../outside.txt")


def test_rejects_deep_traversal(workspace_root):
    with pytest.raises(WorkspaceError):
        Workspace(workspace_root).resolve("sub/../../outside.txt")


def test_allows_dotdot_that_stays_inside(workspace_root):
    (workspace_root / "sub").mkdir()
    assert Workspace(workspace_root).relative("sub/../allowed.txt") == "allowed.txt"


def test_rejects_absolute_path_outside(workspace_root):
    outside = workspace_root.parent / "outside.txt"
    with pytest.raises(WorkspaceError):
        Workspace(workspace_root).resolve(str(outside.resolve()))


def test_allows_absolute_path_inside(workspace_root):
    inside = (workspace_root / "allowed.txt").resolve()
    assert Workspace(workspace_root).resolve(str(inside)) == inside


def test_rejects_symlink_pointing_outside(workspace_root):
    link = workspace_root / "link.txt"
    try:
        os.symlink(workspace_root.parent / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available on this system")
    with pytest.raises(WorkspaceError):
        Workspace(workspace_root).resolve("link.txt")


def test_protected_paths(workspace_root):
    workspace = Workspace(workspace_root)
    assert workspace.is_protected(".git/config")
    assert workspace.is_protected(".forge/tasks/abc/evidence.json")
    assert not workspace.is_protected(".forge/config.toml")
    with pytest.raises(WorkspaceError, match="protected"):
        workspace.ensure_writable(".git/HEAD")
    assert workspace.ensure_writable("new.txt") == (workspace_root / "new.txt").resolve()


def test_walk_files_skips_ignored_directories(workspace_root):
    (workspace_root / "src").mkdir()
    (workspace_root / "src" / "app.py").write_text("x = 1\n")
    (workspace_root / "node_modules").mkdir()
    (workspace_root / "node_modules" / "lib.js").write_text("y\n")
    (workspace_root / ".git").mkdir()
    (workspace_root / ".git" / "HEAD").write_text("ref\n")
    workspace = Workspace(workspace_root)

    files = [workspace.relative(path) for path in workspace.walk_files()]

    assert files == ["allowed.txt", "src/app.py"]


def test_workspace_must_be_a_directory(workspace_root):
    with pytest.raises(WorkspaceError):
        Workspace(workspace_root / "allowed.txt")
