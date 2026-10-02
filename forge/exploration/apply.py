"""Applying a selected candidate to the user's project, safely and reversibly.

Forge does not run `git apply` or merge anything. It writes the candidate's
final version of each file it changed, from the exploration record, and
only after checking every file first:

    the file must still be exactly as it was when exploration started
    (same content hash as the baseline); otherwise the user changed it in
    the meantime, and nothing at all is applied.

Files the candidate did not change are never touched, so the user's other
work is safe. The apply runs as an ordinary Forge task: original contents
are journaled before writing, and the task's proof of work is the selected
candidate's evidence plus the change report of the apply itself.
`forge tasks undo <task_id>` reverts it.
"""

import hashlib

from pydantic import BaseModel, Field

from forge.agent.state import new_task_id
from forge.evidence import TaskEvidence
from forge.exploration.results import AppliedRecord, CandidateResult, ExplorationRun
from forge.exploration.store import ExplorationStore
from forge.fileio import atomic_write_bytes
from forge.git.repo import GitRepository
from forge.tasks.snapshot import compute_changes, file_hash, take_snapshot
from forge.tasks.store import TaskStore
from forge.workspace import Workspace, WorkspaceError


class ApplyError(Exception):
    pass


class ApplyItem(BaseModel):
    path: str
    action: str  # "write" | "delete" | "skip"
    note: str = ""


class ApplyPreview(BaseModel):
    run_id: str
    candidate_id: str
    items: list[ApplyItem] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # files changed since exploration started

    @property
    def ok(self) -> bool:
        return not self.conflicts


def preview_apply(run: ExplorationRun, candidate_id: str, workspace: Workspace) -> ApplyPreview:
    candidate = run.candidate(candidate_id)
    preview = ApplyPreview(run_id=run.run_id, candidate_id=candidate.candidate_id)
    if run.applied is not None:
        preview.conflicts.append(f"run {run.run_id} was already applied (candidate {run.applied.candidate_id}, task {run.applied.task_id})")
        return preview
    if not candidate.files:
        preview.conflicts.append(f"candidate {candidate.candidate_id} changed nothing")
    for delta in candidate.files:
        try:
            target = workspace.ensure_writable(delta.path)
        except WorkspaceError as error:
            preview.conflicts.append(f"{delta.path}: {error}")
            continue
        current = file_hash(target)
        if current == delta.final_hash:
            preview.items.append(ApplyItem(path=delta.path, action="skip", note="already has the candidate's version"))
        elif current != delta.baseline_hash:
            preview.conflicts.append(f"{delta.path}: changed since exploration started")
        else:
            action = "delete" if delta.final_hash is None else "write"
            preview.items.append(ApplyItem(path=delta.path, action=action, note=delta.kind))
    return preview


def apply_candidate(run: ExplorationRun, candidate_id: str, workspace: Workspace, store: ExplorationStore) -> tuple[AppliedRecord, TaskEvidence]:
    preview = preview_apply(run, candidate_id, workspace)
    if not preview.ok:
        raise ApplyError("Nothing was applied: " + "; ".join(preview.conflicts))
    candidate = run.candidate(candidate_id)
    if candidate.evidence is None:
        raise ApplyError(f"Candidate {candidate.candidate_id} has no proof of work; nothing was applied.")
    contents = _final_contents(run, candidate, store)

    tasks = TaskStore(workspace)
    task_id = new_task_id()
    repo = GitRepository.discover(workspace.root)
    snapshot = take_snapshot(workspace, repo)
    tasks.create(task_id)
    tasks.save_checkpoint(task_id, snapshot)
    journal = tasks.journal(task_id)

    written = []
    for item in preview.items:
        if item.action == "skip":
            continue
        target = workspace.ensure_writable(item.path)
        journal.before_write(target)
        if item.action == "delete":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, contents[item.path])
        written.append(item.path)

    changes = compute_changes(snapshot, workspace, repo, journal.pre_images())
    evidence = candidate.evidence.model_copy(
        update={
            "task_id": task_id,
            "task": f"{run.task} (candidate {candidate.candidate_id} of exploration {run.run_id})",
            "changes": changes,
            "files_changed": changes.changed_paths,
            "extra": {
                **candidate.evidence.extra,
                "exploration": {
                    "run_id": run.run_id,
                    "candidate": candidate.candidate_id,
                    "candidate_task_id": candidate.task_id,
                    "selection": run.selection.model_dump(mode="json") if run.selection else None,
                },
            },
        }
    )
    tasks.save_evidence(evidence)
    record = AppliedRecord(candidate_id=candidate.candidate_id, task_id=task_id, files=written)
    run.applied = record
    store.save(run)
    return record, evidence


def _final_contents(run: ExplorationRun, candidate: CandidateResult, store: ExplorationStore) -> dict[str, bytes]:
    """Every file the candidate wrote, read and hash-checked before anything is applied."""
    contents: dict[str, bytes] = {}
    for delta in candidate.files:
        if delta.final_hash is None:
            continue
        data = store.candidate_file(run.run_id, candidate.candidate_id, delta.path)
        if data is None or hashlib.sha256(data).hexdigest() != delta.final_hash:
            raise ApplyError(f"The exploration record for {delta.path} is missing or damaged; nothing was applied.")
        contents[delta.path] = data
    return contents
