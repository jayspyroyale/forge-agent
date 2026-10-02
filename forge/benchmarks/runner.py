"""Benchmark orchestration; no alternate agent or verification implementation."""

import hashlib
import json
import platform
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from forge import __version__
from forge.agent.runtime import run_task
from forge.agent.state import new_task_id
from forge.config import ForgeConfig
from forge.exploration.controller import ExplorationController
from forge.exploration.selection import select
from forge.fileio import atomic_write_text
from forge.security.permissions import PermissionEngine
from forge.security.secret_scan import redact_data
from forge.verification.checks import VerificationCheck


class BenchmarkError(ValueError):
    pass


class BenchmarkSpec(BaseModel):
    name: str
    task: str
    command: str
    constraints: dict = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)


class BenchmarkResult(BaseModel):
    id: str = Field(default_factory=new_task_id)
    benchmark: str
    success: bool
    status: str
    duration_seconds: float
    candidate_count: int
    selected_candidate: str | None
    steps: int
    tokens: int | None
    cost_usd: float | None
    provider: str
    model: str | None
    strategy: str
    starter_sha256: str
    task: str
    verification_command: str
    metadata: dict
    evidence: dict


def builtin_suite() -> Path:
    return Path(__file__).resolve().parent / "tasks"


def load_spec(suite: Path, name: str) -> tuple[BenchmarkSpec, Path]:
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in name):
        raise BenchmarkError("Invalid benchmark name")
    directory = (suite / name).resolve()
    if not directory.is_relative_to(suite.resolve()):
        raise BenchmarkError("Benchmark escapes suite")
    try:
        spec = BenchmarkSpec.model_validate(tomllib.loads((directory / "benchmark.toml").read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"Cannot load benchmark {name}: {error}") from error
    if not (directory / "starter").is_dir():
        raise BenchmarkError(f"Benchmark {name} has no starter")
    for executable in spec.requires:
        if shutil.which(executable) is None:
            raise BenchmarkError(f"Benchmark {name} requires {executable}")
    return spec, directory / "starter"


def starter_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(root.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts:
            if file.is_symlink():
                raise BenchmarkError("Benchmark starters must not contain symlinks")
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


async def run_benchmark(
    config: ForgeConfig,
    name: str,
    *,
    suite: Path | None = None,
    explore: bool = True,
    provider_factory=None,
    on_event=None,
    candidate_events=None,
    permissions=None,
) -> BenchmarkResult:
    spec, starter = load_spec(suite or builtin_suite(), name)
    fingerprint = starter_hash(starter)
    started = time.monotonic()
    engine = permissions or PermissionEngine(config.permissions)
    command = spec.command.replace("{python}", f'"{sys.executable}"')
    check = VerificationCheck(
        name="benchmark acceptance", kind="test", command=command, reason="benchmark's declared acceptance check"
    )
    with tempfile.TemporaryDirectory(prefix="forge-benchmark-") as temp:
        project = Path(temp) / "project"
        shutil.copytree(starter, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        settings = config.model_copy(
            update={
                "workspace": config.workspace.model_copy(update={"root": project}),
                "memory": config.memory.model_copy(update={"enabled": False}),
                "agent": config.agent.model_copy(update={"verification": "auto"}),
                "exploration": config.exploration.model_copy(
                    update={
                        "workspace_dir": Path(temp) / "candidates",
                        "constraints": config.exploration.constraints.model_validate(
                            {**config.exploration.constraints.model_dump(), **spec.constraints}
                        ),
                    }
                ),
            }
        )
        if explore:
            controller = ExplorationController(
                settings,
                permissions=engine,
                provider_factory=provider_factory,
                on_event=on_event,
                candidate_events=candidate_events,
                checks=[check],
            )
            run = await controller.explore(spec.task)
            selection = select(run.comparison, "autonomous")
            controller.record_selection(run, selection)
            outcome = run.final_evidence
            count, chosen = len(run.candidates), selection.candidate_id
            evidence = run.model_dump(mode="json")
            tokens, cost = run.accounted_tokens, run.accounted_cost_usd
            status = run.status
            steps = sum(c.evidence.steps for c in run.candidates if c.evidence)
        else:
            options = {"provider": provider_factory(settings)} if provider_factory else {}
            result = await run_task(settings, spec.task, checks=[check], permissions=engine, **options)
            outcome = result.evidence
            count, chosen, status, steps = 1, None, outcome.status, outcome.steps
            evidence = outcome.model_dump(mode="json")
            tokens = outcome.usage.total_tokens if outcome.usage else None
            cost = outcome.usage.cost_usd if outcome.usage else None
    return BenchmarkResult(
        benchmark=name,
        success=bool(outcome and outcome.status == "completed" and outcome.verified),
        status=status,
        duration_seconds=round(time.monotonic() - started, 3),
        candidate_count=count,
        selected_candidate=chosen,
        steps=steps,
        tokens=tokens,
        cost_usd=cost,
        provider=config.model.provider,
        model=config.model.name,
        strategy="adaptive" if explore and config.exploration.adaptive else ("fixed" if explore else "normal"),
        starter_sha256=fingerprint,
        task=spec.task,
        verification_command=command,
        metadata={
            "forge": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "config": config.model_dump(mode="json"),
            "reproducibility": "API responses are not guaranteed deterministic",
        },
        evidence=evidence,
    )


def export_result(result: BenchmarkResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(redact_data(result.model_dump(mode="json")), indent=2))


def compare_results(paths: list[Path]) -> list[dict]:
    results = [BenchmarkResult.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    return [
        {
            "benchmark": r.benchmark,
            "success": r.success,
            "cost_usd": r.cost_usd,
            "seconds": r.duration_seconds,
            "candidates": r.candidate_count,
            "selected": r.selected_candidate,
            "model": f"{r.provider}:{r.model or 'default'}",
            "starter_sha256": r.starter_sha256,
        }
        for r in results
    ]
