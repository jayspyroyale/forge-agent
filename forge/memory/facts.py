"""Facts Forge can establish from a project's own files, with high confidence.

These are the most trustworthy memories: each one names the file it was
read from, and is re-checked at the start of every task, so when the
project changes (say, from npm to pnpm) the old fact is superseded.
"""

from pathlib import Path

from forge.memory.models import MemoryInput

# Lock file -> (ecosystem, package manager). Order matters when several exist.
_LOCKFILES: list[tuple[str, str, str]] = [
    ("pnpm-lock.yaml", "node", "pnpm"),
    ("yarn.lock", "node", "yarn"),
    ("bun.lockb", "node", "bun"),
    ("bun.lock", "node", "bun"),
    ("package-lock.json", "node", "npm"),
    ("uv.lock", "python", "uv"),
    ("poetry.lock", "python", "poetry"),
    ("Pipfile.lock", "python", "pipenv"),
    ("Cargo.lock", "rust", "cargo"),
    ("go.sum", "go", "go modules"),
]

_LANGUAGES: list[tuple[str, str]] = [
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("requirements.txt", "Python"),
    ("tsconfig.json", "TypeScript"),
    ("package.json", "JavaScript/Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
]


def detect_project_facts(root: Path) -> list[MemoryInput]:
    facts: list[MemoryInput] = []

    seen_ecosystems: set[str] = set()
    for filename, ecosystem, manager in _LOCKFILES:
        if ecosystem in seen_ecosystems or not (root / filename).is_file():
            continue
        seen_ecosystems.add(ecosystem)
        facts.append(_fact(f"package_manager:{ecosystem}", f"Uses {manager} to manage {ecosystem} dependencies", filename))

    languages: list[tuple[str, str]] = []
    for filename, language in _LANGUAGES:
        if (root / filename).is_file() and language not in [name for name, _ in languages]:
            languages.append((language, filename))
    if languages:
        if "TypeScript" in [name for name, _ in languages]:
            languages = [(name, source) for name, source in languages if name != "JavaScript/Node.js"]
        names = ", ".join(name for name, _ in languages)
        sources = ", ".join(sorted({source for _, source in languages}))
        facts.append(_fact("languages", f"Main languages: {names}", sources))

    return facts


def _fact(subject: str, content: str, source_file: str) -> MemoryInput:
    return MemoryInput(
        kind="fact",
        subject=subject,
        content=content,
        source=f"file:{source_file}",
        confidence="high",
        verification="verified",
    )
