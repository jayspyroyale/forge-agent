"""A heuristic classifier for shell commands.

This does NOT understand shell syntax and cannot prove a command is safe. It
catches obviously destructive or escaping patterns so they can be denied or
escalated. It is one layer of defense, alongside the permission policy, the
workspace-confined working directory, timeouts, and secret scrubbing.
"""

import re
import shlex
from pathlib import Path, PureWindowsPath

from forge.security.risk import RiskAssessment, RiskLevel

_RULES: list[tuple[str, str]] = [
    # Recursive or forced deletion
    (r"\brm\s+(-\w*[rR]\w*|--recursive)\b", "recursive deletion (rm -r)"),
    (r"\b(del|erase)\b.*\s/[sS]\b", "recursive deletion (del /s)"),
    (r"\b(rmdir|rd)\b.*\s/[sS]\b", "recursive directory deletion (rmdir /s)"),
    (r"\bRemove-Item\b.*-Recurse", "recursive deletion (Remove-Item -Recurse)"),
    (r"\bshred\b", "secure file destruction (shred)"),
    # Destructive Git operations
    (r"\bgit\s+reset\b.*--hard", "discards changes (git reset --hard)"),
    (r"\bgit\s+clean\b.*\s-\w*[fF]", "deletes untracked files (git clean -f)"),
    (
        r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--mirror\b|\s--delete\b|\s\+\S)",
        "rewrites remote history (git push --force)",
    ),
    (r"\bgit\s+checkout\s+(--\s+)?\.(\s|$)", "discards working tree changes (git checkout .)"),
    (r"\bgit\s+restore\b", "discards working tree changes (git restore)"),
    (r"\bgit\s+(branch|tag)\s+.*-D\b", "force-deletes branches or tags"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "deletes stashed changes"),
    (r"\bgit\s+(filter-branch|filter-repo|update-ref\s+-d)\b", "rewrites repository history"),
    # Disks and the system
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem (mkfs)"),
    (r"\bformat\s+[a-zA-Z]:", "formats a drive"),
    (r"\b(diskpart|fdisk|parted|sfdisk)\b", "edits disk partitions"),
    (r"\bdd\b.*\bof=", "raw disk write (dd of=)"),
    (r">\s*/dev/(sd|hd|nvme|disk)", "writes to a raw device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shuts down or restarts the machine"),
    (r"\b(Stop-Computer|Restart-Computer)\b", "shuts down or restarts the machine"),
    (r"\breg\s+(delete|add)\b", "modifies the Windows registry"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    # Privilege escalation
    (r"(^|[;&|(]\s*)(sudo|su|doas|runas|pkexec)\b", "privilege escalation"),
    (r"\bStart-Process\b.*-Verb\s+RunAs", "privilege escalation"),
    (r"\b(chmod|chown)\s+(-\w*R\w*\s+)?(777|a\+rwx)\b", "makes files world-writable"),
    (r"\bchown\s+-\w*R", "recursively changes ownership"),
    # Downloading and executing code
    (
        r"\b(curl|wget|iwr|Invoke-WebRequest)\b.*\|\s*(sh|bash|zsh|python\d?|iex|Invoke-Expression)\b",
        "runs code downloaded from the internet",
    ),
    # Forge's own records and Git internals
    (r"\.forge[/\\]tasks", "touches Forge's task records"),
    (r"\.forge[/\\](explorations|benchmarks)", "touches Forge's evidence records"),
    (r"(^|[\s\"'=])\.git[/\\]", "touches Git internals directly"),
]

_COMPILED = [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _RULES]


def classify_command(command: str, workspace_root: Path | None = None) -> RiskAssessment:
    """Return DANGEROUS with reasons for risky-looking commands, otherwise EXECUTE."""
    reasons = [reason for pattern, reason in _COMPILED if pattern.search(command)]
    if workspace_root is not None:
        reasons += _paths_outside_workspace(command, workspace_root)
    if reasons:
        return RiskAssessment(level=RiskLevel.DANGEROUS, reasons=_unique(reasons))
    return RiskAssessment(level=RiskLevel.EXECUTE)


_SEPARATORS = {"&&", "||", "|", ";", "&"}
_WINDOWS_SWITCH = re.compile(r"^/[A-Za-z?]{1,3}$")


def _paths_outside_workspace(command: str, workspace_root: Path) -> list[str]:
    """Arguments that point outside the workspace (absolute paths, `..` escapes, `~`).

    The program itself is skipped (interpreters often live outside the
    project, e.g. /usr/bin/python3), as are Windows-style switches like /s.
    """
    root = workspace_root.resolve()
    reasons = []
    command_position = True
    for token in _tokens(command):
        if token in _SEPARATORS:
            command_position = True
            continue
        if command_position:
            command_position = False
            continue
        candidate = token.strip("\"'")
        if _WINDOWS_SWITCH.match(candidate):
            continue
        # Values in --flag=value or key=value form.
        if "=" in candidate and not candidate.startswith(("/", "~")):
            candidate = candidate.split("=", 1)[1]
        if not candidate or candidate.startswith("-"):
            continue
        if candidate.startswith("~"):
            reasons.append(f"refers to a path outside the workspace: {token}")
            continue
        looks_absolute = candidate.startswith(("/", "\\")) or PureWindowsPath(candidate).drive != ""
        has_parent_step = ".." in Path(candidate).parts or ".." in PureWindowsPath(candidate).parts
        if not (looks_absolute or has_parent_step):
            continue
        if candidate.startswith("/dev/null") or candidate.upper() == "NUL":
            continue
        try:
            resolved = (root / candidate.replace("\\", "/")).resolve()
        except (OSError, ValueError):
            continue
        if not resolved.is_relative_to(root):
            reasons.append(f"refers to a path outside the workspace: {token}")
    return reasons


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
