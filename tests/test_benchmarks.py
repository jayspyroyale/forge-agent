import asyncio
import json

import pytest
from exploration_helpers import permissive, plans_reply

from forge.benchmarks.demo import run_demo
from forge.benchmarks.runner import (
    BenchmarkError,
    builtin_suite,
    compare_results,
    export_result,
    load_spec,
    run_benchmark,
)
from forge.config import ForgeConfig
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall


def factory(config):
    if config.workspace_root.name == "project":
        return FakeModelProvider(config, responses=[plans_reply("Direct multiplication", "Named product")])
    return FakeModelProvider(
        config,
        responses=[
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="edit_file",
                        arguments={"path": "app.py", "old_text": "return a + b", "new_text": "return a * b"},
                    )
                ]
            ),
            "done",
        ],
    )


def test_benchmark_runs_real_candidates_exports_and_compares(tmp_path):
    config = ForgeConfig(provider="fake", workspace=tmp_path)
    result = asyncio.run(run_benchmark(config, "fix_python_bug", provider_factory=factory, permissions=permissive()))
    assert result.success and result.candidate_count == 2
    assert result.selected_candidate in {"A", "B"}
    assert result.steps == 4 and len(result.starter_sha256) == 64
    assert result.evidence["candidates"][0]["evidence"]["checks"][0]["outcome"] == "verified"
    path = tmp_path / "result.json"
    export_result(result, path)
    assert json.loads(path.read_text())["metadata"]["reproducibility"]
    assert compare_results([path])[0]["success"]


def test_normal_benchmark_and_failed_verification(tmp_path):
    def wrong(config):
        return FakeModelProvider(
            config,
            responses=[
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="e",
                            name="edit_file",
                            arguments={"path": "app.py", "old_text": "return a + b", "new_text": "return a - b"},
                        )
                    ]
                ),
                "done",
            ],
        )

    result = asyncio.run(
        run_benchmark(
            ForgeConfig(provider="fake", workspace=tmp_path, verification_attempts=1),
            "fix_python_bug",
            explore=False,
            provider_factory=wrong,
            permissions=permissive(),
        )
    )
    assert not result.success and result.status == "verification_failed"


def test_suite_contains_reproducible_tasks_and_rejects_traversal():
    assert len(list(builtin_suite().glob("*/benchmark.toml"))) == 5
    with pytest.raises(BenchmarkError):
        load_spec(builtin_suite(), "../outside")


def test_offline_demo_verifies_selects_applies_and_undoes():
    run = asyncio.run(run_demo())
    assert run.selection.candidate_id == "A"
    assert run.candidate("B").status == "verification_failed"
    assert run.applied and run.accounted_cost_usd > 0


def test_benchmark_cli_list():
    from typer.testing import CliRunner
    from forge.cli import app

    result = CliRunner().invoke(app, ["benchmark", "list"])
    assert result.exit_code == 0 and "fix_python_bug" in result.output
