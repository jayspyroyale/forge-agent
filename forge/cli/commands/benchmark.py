"""Research runs and JSON exports."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from forge.benchmarks.runner import BenchmarkError, builtin_suite, compare_results, export_result, run_benchmark
from forge.cli.approval import make_cli_approver
from forge.cli.explore_render import ExplorationRenderer
from forge.cli.output import console, fail
from forge.cli.settings import load_cli_config
from forge.security.permissions import PermissionEngine

benchmark_app = typer.Typer(help="Run reproducible coding benchmarks and compare JSON results.", no_args_is_help=True)


@benchmark_app.command("list")
def list_benchmarks():
    for directory in sorted(builtin_suite().iterdir()):
        if (directory / "benchmark.toml").is_file():
            console.print(directory.name)


@benchmark_app.command("run")
def run(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Benchmark name; see benchmark list.")] = "fix_python_bug",
    explore: Annotated[bool, typer.Option("--explore/--normal")] = True,
    approaches: Annotated[int | None, typer.Option("--approaches", "-n")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    provider: Annotated[str | None, typer.Option("--provider", "-p")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
):
    config = load_cli_config(
        ctx, **{"model.provider": provider, "model.name": model, "exploration.approaches": approaches}
    ).config
    renderer = ExplorationRenderer(console, "normal")
    try:
        result = asyncio.run(
            run_benchmark(
                config,
                name,
                explore=explore,
                on_event=renderer,
                candidate_events=renderer.candidate_handler,
                permissions=PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes)),
            )
        )
    except (BenchmarkError, ValueError) as error:
        raise fail(str(error))
    path = output or config.workspace_root / ".forge" / "benchmarks" / f"{result.id}.json"
    export_result(result, path)
    console.print(
        f"{name}: {'PASS' if result.success else 'FAIL'}; {result.candidate_count} candidates; "
        f"{result.duration_seconds:.1f}s; selected {result.selected_candidate or '-'}"
    )
    console.print(f"Result: {path}")
    if not result.success:
        raise typer.Exit(code=1)


@benchmark_app.command("compare")
def compare(paths: Annotated[list[Path], typer.Argument(help="JSON result files.")]):
    try:
        rows = compare_results(paths)
    except (OSError, ValueError) as error:
        raise fail(str(error))
    table = Table(title="Benchmark results (API runs are not deterministic)")
    for name in ("Task", "Model", "Pass", "Cost", "Seconds", "Candidates", "Selected"):
        table.add_column(name)
    for row in rows:
        table.add_row(
            row["benchmark"],
            row["model"],
            str(row["success"]),
            str(row["cost_usd"] if row["cost_usd"] is not None else "unknown"),
            str(row["seconds"]),
            str(row["candidates"]),
            row["selected"] or "-",
        )
    console.print(table)
    if len({row["starter_sha256"] for row in rows}) > 1:
        console.print("Starter hashes differ; these runs do not share an identical baseline.")
