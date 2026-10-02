"""Finding files that are probably relevant to a task, without embeddings.

Deterministic signals only:

    name     a word from the task appears in the file's path    (strongest)
    text     words from the task appear inside the file
    recent   the file has uncommitted changes in Git

The result is a short list of paths with the reason each was picked. Forge
gives the agent this list as a hint (with its provenance); the agent still
reads the files itself through its tools.
"""

import re
from pathlib import Path

from pydantic import BaseModel, Field

from forge.fileio import looks_binary
from forge.git.repo import GitError, GitRepository
from forge.workspace import Workspace

MAX_FILES_SCANNED = 3_000
MAX_FILE_BYTES = 256_000
MIN_WORD_LENGTH = 3

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
STOPWORDS = frozenset(
    """
    the and for with that this from into make add fix use using all any are but can not was were has have had
    its it's our out get set new now then than when what which who why how should would could will shall
    file files code test tests function functions class method methods please task need needs want some
    implement improve change update create remove delete write read run add support project repo work
    """.split()
)


class RelevantFile(BaseModel):
    path: str
    score: float
    reasons: list[str] = Field(default_factory=list)


def task_keywords(task: str) -> list[str]:
    """Lowercase words from the task, split on snake_case and camelCase, without filler words."""
    words: list[str] = []
    for token in _WORD.findall(task):
        parts = [token, *token.split("_"), *_CAMEL.findall(token)]
        for part in parts:
            word = part.lower()
            if len(word) >= MIN_WORD_LENGTH and word not in STOPWORDS and word not in words:
                words.append(word)
    return words


def find_relevant_files(workspace: Workspace, task: str, limit: int = 8) -> list[RelevantFile]:
    keywords = task_keywords(task)
    if not keywords or limit <= 0:
        return []
    recent = _recently_changed(workspace)
    found: list[RelevantFile] = []

    for index, path in enumerate(workspace.walk_files()):
        if index >= MAX_FILES_SCANNED:
            break
        relative = workspace.relative(path)
        lowered = relative.lower()
        score = 0.0
        reasons: list[str] = []

        name_hits = [word for word in keywords if word in lowered]
        if name_hits:
            score += 3 * len(name_hits)
            reasons.append("name matches " + ", ".join(repr(word) for word in name_hits))

        text_hits = _text_hits(path, keywords)
        if text_hits:
            score += len(text_hits)
            reasons.append("mentions " + ", ".join(repr(word) for word in text_hits[:5]))

        if relative in recent and score > 0:
            score += 2
            reasons.append("has uncommitted changes")

        if score > 0:
            found.append(RelevantFile(path=relative, score=score, reasons=reasons))

    found.sort(key=lambda item: (-item.score, item.path))
    return found[:limit]


def format_relevant_files(files: list[RelevantFile]) -> str:
    lines = [
        "[Forge context] Files that may be relevant to this task, found by name and text search. "
        "This is a hint, not a fact: read the files before relying on them."
    ]
    lines += [f"- {item.path} ({'; '.join(item.reasons)})" for item in files]
    return "\n".join(lines)


def _text_hits(path: Path, keywords: list[str]) -> list[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if looks_binary(data):
        return []
    text = data.decode("utf-8", errors="replace").lower()
    return [word for word in keywords if word in text]


def _recently_changed(workspace: Workspace) -> set[str]:
    repo = GitRepository.discover(workspace.root)
    if repo is None:
        return set()
    try:
        status = repo.status()
    except GitError:
        return set()
    changed = set()
    for entry in status.entries:
        absolute = (repo.root / entry.path).resolve()
        if absolute.is_relative_to(workspace.root):
            changed.add(absolute.relative_to(workspace.root).as_posix())
    return changed
