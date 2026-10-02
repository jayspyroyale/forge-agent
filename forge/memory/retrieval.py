"""Choosing which memories to show the agent for a task.

Never the whole database. Instructions and preferences (how the user wants
work done) are always candidates; other memories need to relate to the
task by keyword. Low-confidence memories are only included when they match.
The order is deterministic.
"""

from forge.context.retrieval import task_keywords
from forge.memory.models import CONFIDENCE_RANK, MemoryRecord

ALWAYS_RELEVANT = ("instruction", "preference")
KIND_WEIGHT = {"instruction": 3, "preference": 2, "architecture": 2, "decision": 1, "workflow": 1, "fact": 1}


def relevant_memories(records: list[MemoryRecord], task: str, limit: int = 8) -> list[MemoryRecord]:
    keywords = task_keywords(task)
    scored: list[tuple[float, MemoryRecord]] = []
    for record in records:
        if not record.usable():
            continue
        text = f"{record.subject or ''} {record.content}".lower()
        matches = sum(1 for word in keywords if word in text)
        always = record.kind in ALWAYS_RELEVANT
        if not always and record.kind != "fact" and matches == 0:
            continue
        if record.confidence == "low" and matches == 0:
            continue
        score = 3 * matches + KIND_WEIGHT[record.kind] + CONFIDENCE_RANK[record.confidence]
        scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], -item[1].updated_at.timestamp(), item[1].id))
    return [record for _, record in scored[:limit]]


def format_memories(records: list[MemoryRecord]) -> str:
    lines = [
        "[Forge memory] What Forge remembers about this project, with where it came from. "
        "Prefer the current files when they disagree; treat low-confidence items as hints."
    ]
    for record in records:
        lines.append(f"- [{record.kind}; {record.confidence} confidence; from {record.source}] {record.content}")
    return "\n".join(lines)
