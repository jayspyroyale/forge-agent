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
    ExplorationDecision,
    ExplorationStarted,
    PlansReady,
)
from forge.exploration.isolation import ExplorationError, Isolation, preflight
from forge.exploration.planner import ApproachPlanner, PlanningError, project_overview
from forge.exploration.plans import ApproachPlan, ModelChoice, candidate_brief, candidate_ids
from forge.exploration.results import CandidateResult, ExplorationRun, PermissionSummary, Selection
from forge.exploration.review import review_candidates
from forge.exploration.store import ExplorationStore
from forge.models.base import ModelProvider
from forge.models.budget import BudgetExhausted, BudgetProvider, ExplorationBudget
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
        count = approaches if approaches is not None else self.config.exploration.approaches
        if not 1 <= count <= MAX_APPROACHES:
            raise ExplorationError(f"approaches must be between 1 and {MAX_APPROACHES} (got {count})")
        baseline = preflight(self.workspace, include_uncommitted)

        run = ExplorationRun(run_id=new_task_id(), task=task, baseline=baseline)
        settings = self.config.exploration
        run.settings = self.config.model_dump(mode="json")
        costs = [v for v in (settings.max_api_cost, settings.budget_usd) if v is not None]
        self.budget = ExplorationBudget(max_tokens=settings.max_tokens,
            max_cost=min(costs) if costs else None, max_seconds=settings.max_elapsed_time)
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
            initial = min(count, settings.initial_approaches) if settings.adaptive else count
            plans = await self._plan(run, task, initial)
            run.status = "running"
            self.store.save(run)
            plateau = 0
            best = None
            while plans:
                for plan in plans:
                    self.budget.check()
                    await self._run_candidate(run, plan, isolation, keep_workspaces)
                run.comparison = compare(run.candidates, settings.weights, settings.constraints)
                self.budget.check()
                if not settings.adaptive or len(run.candidates) >= count:
                    run.stop_reason = "maximum candidates reached"
                    break
                ranking = run.comparison.ranking
                winner = run.candidate(ranking[0]) if ranking else None
                checks = run.comparison.measured_for(winner.candidate_id).checks if winner else {}
                strong = bool(checks) and all(v == "verified" for v in checks.values())
                if strong and (len(ranking) == 1 or (
                    run.comparison.score(ranking[0]).total - run.comparison.score(ranking[1]).total
                    >= settings.dominance_margin)):
                    run.stop_reason = "strong verified candidate dominates"
                    break
                quality = (len(ranking), -(winner.additions + winner.deletions)) if winner else (0, 0)
                plateau = plateau + 1 if best is not None and quality <= best else 0
                best = max(best, quality) if best is not None else quality
                if plateau >= settings.plateau_rounds:
                    run.stop_reason = "improvement plateau reached"
                    break
                parents = ranking[:2] if settings.experimental_generation else []
                next_task = task
                if parents:
                    next_task += "\nDesign a new implementation informed by this measured evidence; do not merge code:\n"
                    next_task += "\n".join(f"{c.candidate_id}: {c.plan.summary}; checks={c.checks}; files={c.files_changed}; "
                        f"dependencies={c.dependencies_added}" for c in run.candidates if c.candidate_id in parents)
                self._emit(ExplorationDecision(action="continue", reason="Results are weak or close; trying another candidate"))
                plans = await self._plan(run, next_task, 1, parents=parents)
                if not plans:
                    run.stop_reason = "planner produced no additional distinct approach"
            if settings.adaptive:
                self._emit(ExplorationDecision(action="stop", reason=run.stop_reason or "exploration complete"))
            await self.evaluate(run)
            run.status = "completed"
        except BudgetExhausted as error:
            run.stop_reason = str(error)
            run.notes.append(str(error))
            run.comparison = compare(run.candidates, settings.weights, settings.constraints)
            run.status = "completed" if run.candidates else "failed"
        except PlanningError as error:
            run.status, run.error = "failed", str(error)
            raise ExplorationError(str(error)) from error
        except BaseException:
            run.status = "cancelled" if run.status in ("planning", "running") else run.status
            run.error = run.error or "exploration was interrupted"
            raise
        finally:
            run.finished_at = datetime.now(UTC)
            run.accounted_tokens = self.budget.tokens
            run.accounted_cost_usd = None if self.budget.unknown_cost else self.budget.cost
            run.model_calls = self.budget.calls
            run.estimated_usage_calls = self.budget.estimated_calls
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
            result = await review_candidates(self._provider(self.config), run.task, run.candidates, patches)
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

    def _provider(self, config: ForgeConfig) -> ModelProvider:
        provider = self.provider_factory(config)
        return BudgetProvider(provider, self.budget) if hasattr(self, "budget") else provider

    async def _plan(self, run: ExplorationRun, task: str, count: int, *, parents=()) -> list[ApproachPlan]:
        settings = self.config.exploration
        planner = ApproachPlanner(self._provider(self.config))
        if settings.strategy == "same_approach" and run.plans:
            plans = [run.plans[0].model_copy(update={"id": cid}) for cid in candidate_ids(count, len(run.plans))]
            usage, notes = None, []
        else:
            planned_count = 1 if settings.strategy == "same_approach" else count
            result = await planner.plan(task, planned_count, overview=project_overview(self.workspace, task),
                existing=run.plans, start_index=len(run.plans))
            plans, usage, notes = result.plans, result.usage, result.notes
            if settings.strategy == "same_approach" and plans:
                plans = [plans[0].model_copy(update={"id": cid}) for cid in candidate_ids(count, len(run.plans))]
        for index, plan in enumerate(plans, start=len(run.plans)):
            choice = settings.candidate_models.get(plan.id)
            if choice is None and settings.model_pool and settings.model_assignment != "explicit":
                choice = settings.model_pool[index % len(settings.model_pool)]
            if choice is not None:
                plan.model = ModelChoice(**choice.model_dump(include={"provider", "name", "base_url"}))
            if parents:
                plan.parents = list(parents)
                plan.generation = 1 + max(run.candidate(cid).plan.generation for cid in parents)
        run.plans += plans
        if usage is not None:
            run.planning_usage = usage if run.planning_usage is None else run.planning_usage + usage
        run.notes += notes
        self._emit(PlansReady(plans=plans, notes=notes, round=max((p.generation for p in plans), default=1)))
        return plans

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
            provider=self._provider(config),
            permissions=self.permissions,
            checks=detect_checks(root, environment_root=self.workspace.root),
            on_event=handler,
            operation_guard=self.budget.check,
            deadline=self.budget.deadline,
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
            settings = config.exploration
            full = settings.candidate_models.get(plan.id)
            if full is None and settings.model_pool and settings.model_assignment != "explicit":
                index = candidate_ids(MAX_APPROACHES).index(plan.id)
                full = settings.model_pool[index % len(settings.model_pool)]
            if full is not None:
                chosen = full.model_dump()
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
