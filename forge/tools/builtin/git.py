"""Read-only Git tools: let the model see what has changed without running git itself."""

from pydantic import Field

from forge.git.repo import GitError, GitRepository
from forge.security.risk import RiskLevel
from forge.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

STATUS_LABELS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "U": "conflict", "T": "type changed"}


def _repository(context: ToolContext) -> GitRepository:
    repo = GitRepository.discover(context.workspace.root)
    if repo is None:
        raise ToolError("The workspace is not inside a Git repository.")
    return repo


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the Git branch and which files are modified, staged, or untracked. Read-only."
    Args = NoArgs
    risk = RiskLevel.READ
    context_source = "git"

    def execute(self, args: NoArgs, context: ToolContext) -> ToolResult:
        repo = _repository(context)
        try:
            status = repo.status()
        except GitError as error:
            raise ToolError(f"git status failed: {error}") from None
        lines = [f"branch: {status.branch or '(detached or no commits)'}"]
        if status.clean:
            lines.append("working tree clean")
        for entry in status.entries:
            if entry.untracked:
                label = "untracked"
            else:
                codes = {code for code in (entry.index, entry.worktree) if code.strip()}
                label = ", ".join(sorted(STATUS_LABELS.get(code, code) for code in codes))
                if entry.index.strip():
                    label += " (staged)"
            lines.append(f"{label}: {entry.path}")
        return ToolResult.ok(
            "\n".join(lines),
            branch=status.branch,
            clean=status.clean,
            modified=status.modified,
            untracked=status.untracked,
        )


class GitDiffArgs(ToolArgs):
    path: str | None = Field(default=None, description="Limit the diff to this file or directory.")
    max_chars: int = Field(default=8000, ge=500, le=50_000, description="Maximum characters of diff text to return.")


class GitDiffTool(Tool):
    name = "git_diff"
    description = (
        "Show changes in tracked files compared with the last commit (staged and unstaged), with a per-file "
        "+/- summary. Untracked files are not included. Read-only."
    )
    Args = GitDiffArgs
    risk = RiskLevel.READ
    context_source = "git"

    def execute(self, args: GitDiffArgs, context: ToolContext) -> ToolResult:
        repo = _repository(context)
        paths = None
        if args.path:
            target = context.workspace.resolve(args.path)
            paths = [target.relative_to(repo.root).as_posix() or "."]
        try:
            diff = repo.diff_summary(paths, max_chars=args.max_chars)
        except GitError as error:
            raise ToolError(f"git diff failed: {error}") from None
        if not diff.files:
            return ToolResult.ok("No changes in tracked files.", additions=0, deletions=0, files=[])
        summary = [
            f"{f.path}: +{f.additions if f.additions is not None else '?'} -{f.deletions if f.deletions is not None else '?'}"
            for f in diff.files
        ]
        output = "\n".join(summary) + f"\ntotal: +{diff.additions} -{diff.deletions}\n\n" + diff.text
        return ToolResult.ok(
            output,
            additions=diff.additions,
            deletions=diff.deletions,
            files=[f.path for f in diff.files],
            truncated=diff.truncated,
        )
