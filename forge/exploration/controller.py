"""The exploration controller: one task, several independent real implementations.

                    ExplorationController
                   /          |           \\
            Candidate A   Candidate B   Candidate C
                 |             |             |
             run_task      run_task      run_task        <- the normal Forge runtime
                 |             |             |
            workspace A   workspace B   workspace C      <- isolated copies (forge.exploration.isolation)

The controller does not contain an agent. Each candidate is an ordinary
`run_task` call (same provider layer, tools, permission engine,
verification, context engine, memory, and proof of work), pointed at its
own isolated workspace and given an approach plan to implement. The
controller only plans, isolates, runs, and measures.

One candidate failing (or crashing) never stops the others. The user's
project is never modified during exploration; applying a chosen candidate
is a separate, explicit step (forge.exploration.apply).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from forge.agent.events import EventHandler
from forge.agent.runtime import memory_project, run_task, workspace_from_config
from forge.agent.state import new_task_id
from forge.config import ForgeConfig
from forge.config.loader import user_config_dir
from forge.exploration.changes import compare as compare_files
from forge.exploration.changes import describe, manifest
from forge.exploration.compare import Comparison, compare
from forge.exploration.dependencies import added_dependencies
from forge.exploration.events import (
    CandidateFinished,
    CandidateStarted,
    ExplorationEvent,
    ExplorationEventHandler,
    ExplorationFinished,
    ExplorationStarted,
    PlansReady,
)
from forge.exploration.isolation import ExplorationError, Isolation, preflight
from forge.exploration.planner import ApproachPlanner, PlanningError, project_overview
from forge.exploration.plans import ApproachPlan, candidate_brief
from forge.exploration.results import CandidateResult, ExplorationRun, PermissionSummary, Selection
from forge.exploration.review import review_candidates
from forge.exploration.store import ExplorationStore
from forge.models.base import ModelProvider
from forge.models.registry import create_provider
from forge.security.permissions import PermissionEngine
from forge.verification.detect import detect_checks

MAX_APPROACHES = 10  # hard limit: every candidate is a full agent run


class ExplorationController:
    def __init__(
        self,
        config: ForgeConfig,
        *,
        permissions: PermissionEngine | None = None,
        provider_factory: Callable[[ForgeConfig], ModelProvider] | None = None,
        on_event: ExplorationEventHandler | None = None,
        candidate_events: Callable[[str], EventHandler | None] | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace_from_config(config)
        self.permissions = permissions or PermissionEngine(config.permissions)
        # Looked up at call time, so the provider registry stays the single place providers come from.
        self.provider_factory = provider_factory or (lambda candidate_config: create_provider(candidate_config))
        self.on_event = on_event
        self.candidate_events = candidate_events
        self.store = ExplorationStore(self.workspace)

    @property
    def work_dir(self) -> Path:
        return Path(self.config.exploration.workspace_dir or user_config_dir() / "worktrees")

    async def explore(
        self,
        task: str,
        approaches: int | None = None,
        *,
        include_uncommitted: bool = True,
        keep_workspaces: bool = False,
    ) -> ExplorationRun:
        count = approaches or self.config.exploration.approaches
        if not 1 <= count <= MAX_APPROACHES:
            raise ExplorationError(f"approaches must be between 1 and {MAX_APPROACHES} (got {count})")
        baseline = preflight(self.workspace, include_uncommitted)

        run = ExplorationRun(run_id=new_task_id(), task=task, baseline=baseline)
        records = self.store.create(run.run_id)
        self.store.save(run)
        self._emit(
            ExplorationStarted(
                run_id=run.run_id,
                task=task,
                approaches=count,
                baseline_kind=baseline.kind,
                uncommitted=len(baseline.uncommitted),
                left_out=len(baseline.left_out),
            )
        )
        isolation = Isolation(baseline, self.work_dir / run.run_id, records)
        try:
            isolation.prepare()
            plans = await self._plan(run, task, count)
            run.status = "running"
            self.store.save(run)
            for plan in plans:
                await self._run_candidate(run, plan, isolation, keep_workspaces)
            await self.evaluate(run)
            run.status = "completed"
        except PlanningError as error:
            run.status, run.error = "failed", str(error)
            raise ExplorationError(str(error)) from error
        except BaseException:
            run.status = "cancelled" if run.status in ("planning", "running") else run.status
            run.error = run.error or "exploration was interrupted"
            raise
        finally:
            run.finished_at = datetime.now(UTC)
            self.store.save(run)
            isolation.cleanup()
        self._emit(ExplorationFinished(run=run))
        return run

    async def evaluate(self, run: ExplorationRun, review: bool | None = None) -> Comparison:
        """Compare the candidates by the configured policy. A model review runs only when asked for or needed."""
        settings = self.config.exploration
        if review is None:
            weighted = settings.weights.maintainability > 0 or settings.weights.scalability > 0
            review = settings.review == "always" or (settings.review == "auto" and weighted)
        assessments = []
        if review:
            patches = {c.candidate_id: self.store.patch(run.run_id, c.candidate_id) for c in run.candidates}
            result = await review_candidates(self.provider_factory(self.config), run.task, run.candidates, patches)
            assessments = result.assessments
            if result.usage is not None:
                run.review_usage = result.usage if run.review_usage is None else run.review_usage + result.usage
            if result.note:
                run.notes.append(result.note)
        run.comparison = compare(run.candidates, settings.weights, settings.constraints, assessments)
        self.store.save(run)
        return run.comparison

    def record_selection(self, run: ExplorationRun, selection: Selection) -> None:
        run.selection = selection
        self.store.save(run)

    # --- steps ----------------------------------------------------------------------------

    async def _plan(self, run: ExplorationRun, task: str, count: int) -> list[ApproachPlan]:
        planner = ApproachPlanner(self.provider_factory(self.config))
        result = await planner.plan(task, count, overview=project_overview(self.workspace, task), start_index=len(run.plans))
        run.plans += result.plans
        if result.usage is not None:
            run.planning_usage = result.usage if run.planning_usage is None else run.planning_usage + result.usage
        run.notes += result.notes
        self._emit(PlansReady(plans=result.plans, notes=result.notes))
        return result.plans

    async def _run_candidate(
        self, run: ExplorationRun, plan: ApproachPlan, isolation: Isolation, keep: bool
    ) -> CandidateResult:
        started = time.monotonic()
        workspace = None
        result = CandidateResult(candidate_id=plan.id, plan=plan, status="crashed")
        try:
            workspace = isolation.create(plan.id)
            result.workspace, result.workspace_kind = workspace.container, workspace.kind
            self._emit(CandidateStarted(candidate_id=plan.id, plan=plan, workspace=workspace.container))
            result = await self._execute(run, plan, isolation, Path(workspace.root), result)
        except Exception as error:  # one candidate's crash must not stop the others
            result.status = "crashed"
            result.errors.append(f"{type(error).__name__}: {error}")
        finally:
            result.duration_seconds = round(time.monotonic() - started, 3)
            if workspace is not None:
                if keep:
                    result.kept = True
                else:
                    isolation.remove(workspace)
            run.candidates.append(result)
            self.store.save(run)
        self._emit(CandidateFinished(result=result))
        return result

    async def _execute(
        self, run: ExplorationRun, plan: ApproachPlan, isolation: Isolation, root: Path, result: CandidateResult
    ) -> CandidateResult:
        before = manifest(root)
        config = self._candidate_config(root, plan)
        handler = self.candidate_events(plan.id) if self.candidate_events else None
        outcome = await run_task(
            config,
            candidate_brief(run.task, plan),
            provider=self.provider_factory(config),
            permissions=self.permissions,
            checks=detect_checks(root, environment_root=self.workspace.root),
            on_event=handler,
        )

        deltas, patch = describe(compare_files(before, root), root, isolation.baseline_content)
        for delta in deltas:
            if delta.final_hash is not None:
                self.store.save_candidate_file(run.run_id, plan.id, delta.path, (root / delta.path).read_bytes())
        self.store.save_patch(run.run_id, plan.id, patch)

        def final(path: str) -> bytes | None:
            file = root / path
            return file.read_bytes() if file.is_file() else None

        state, evidence = outcome.state, outcome.evidence
        return result.model_copy(
            update={
                "status": state.status,
                "task_id": state.task_id,
                "evidence": evidence,
                "files": deltas,
                "dependencies_added": added_dependencies([d.path for d in deltas], isolation.baseline_content, final),
                "usage": state.usage,
                "cost_usd": state.usage.cost_usd if state.usage is not None else None,
                "permissions": _permission_summary(evidence),
                "errors": [state.error] if state.error else [],
                "provider": evidence.provider,
                "model": evidence.model,
            }
        )

    def _candidate_config(self, root: Path, plan: ApproachPlan) -> ForgeConfig:
        config = self.config
        updates: dict = {
            "workspace": config.workspace.model_copy(update={"root": root}),
            # Candidates share the project's memory, not a memory of their temporary copy.
            "memory": config.memory.model_copy(update={"project": memory_project(config)}),
        }
        if plan.model is not None:
            chosen = {key: value for key, value in plan.model.model_dump().items() if value is not None}
            updates["model"] = config.model.model_copy(update=chosen)
        return config.model_copy(update=updates)

    def _emit(self, event: ExplorationEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)


def _permission_summary(evidence) -> PermissionSummary:
    usage = evidence.tool_usage
    return PermissionSummary(
        approved_by_user=sum(1 for item in usage if item.decided_by == "user" and not item.denied),
        denied=sum(1 for item in usage if item.denied),
        dangerous_attempts=sum(1 for item in usage if item.risk == "dangerous"),
    )
