"""Internal records must stay local, even when .forge paths are replaced by links."""

from pathlib import Path

from forge.workspace import WorkspaceError


def record_path(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceError("Record path must be relative without parent traversal")
    target = root / relative
    if not target.resolve().is_relative_to(root):
        raise WorkspaceError("Record path escapes the workspace")
    current = target
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise WorkspaceError("Record paths must not contain symlinks or junctions")
        current = current.parent
    return target
