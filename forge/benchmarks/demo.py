"""Offline demo of three real implementations, verification, selection, apply and undo.

The model replies are scripted; tools and verification run for real.
All edits are confined to a temporary calculator project.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from forge.benchmarks.runner import builtin_suite
from forge.cli.explore_render import ExplorationRenderer, show_comparison
from forge.cli.output import console, make_streams_safe
from forge.config import ForgeConfig
from forge.exploration.apply import apply_candidate
from forge.exploration.controller import ExplorationController
from forge.exploration.selection import select
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall, Usage
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.tasks.store import TaskStore
from forge.tasks.undo import apply_undo, plan_undo
from forge.verification.checks import VerificationCheck


async def run_demo():
    make_streams_safe()
    import sys

    with tempfile.TemporaryDirectory(prefix="forge-demo-") as temp:
        project = Path(temp) / "project"
        shutil.copytree(
            builtin_suite() / "fix_python_bug" / "starter", project, ignore=shutil.ignore_patterns("__pycache__")
        )
        original = (project / "app.py").read_bytes()
        config = ForgeConfig(
            model={"provider": "fake", "input_cost_per_million": 1, "output_cost_per_million": 2},
            workspace=project,
            memory={"enabled": False},
            agent={"verification_attempts": 1},
            exploration={"approaches": 3, "workspace_dir": Path(temp) / "candidates", "selection_mode": "autonomous",
                         "weights": {"speed": 0}},
        )
        usage = Usage(input_tokens=100, output_tokens=20)

        def factory(settings):
            if settings.workspace_root == project:
                plans = {
                    "approaches": [
                        {"title": title, "summary": summary, "complexity": "low"}
                        for title, summary in [
                            ("Direct multiplication", "Use the multiplication operator directly."),
                            (
                                "Subtraction experiment",
                                "Try subtraction; verification should reject this deliberate wrong answer.",
                            ),
                            ("Named product", "Calculate a named product before returning it."),
                        ]
                    ]
                }
                return FakeModelProvider(settings, responses=[ModelResponse(content=json.dumps(plans), usage=usage)])
            replacement = {"A": "return a * b", "B": "return a - b", "C": "product = a * b\n    return product"}[
                settings.workspace_root.name
            ]
            return FakeModelProvider(
                settings,
                responses=[
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="edit",
                                name="edit_file",
                                arguments={"path": "app.py", "old_text": "return a + b", "new_text": replacement},
                            )
                        ],
                        usage=usage,
                    ),
                    ModelResponse(content="Implementation ready for Forge verification.", usage=usage),
                ],
            )

        renderer = ExplorationRenderer(console, "normal")
        controller = ExplorationController(
            config,
            provider_factory=factory,
            permissions=PermissionEngine(PermissionPolicy.permissive()),
            on_event=renderer,
            candidate_events=renderer.candidate_handler,
            checks=[
                VerificationCheck(
                    name="acceptance", kind="test", command=f'"{sys.executable}" verify.py', reason="demo acceptance"
                )
            ],
        )
        run = await controller.explore("Fix multiply, comparing three approaches")
        assert (project / "app.py").read_bytes() == original
        show_comparison(console, run)
        choice = select(run.comparison, "autonomous")
        controller.record_selection(run, choice)
        record, evidence = apply_candidate(run, choice.candidate_id, controller.workspace, controller.store)
        assert evidence.verified and b"return a * b" in (project / "app.py").read_bytes()
        console.print(
            f"Applied {choice.candidate_id}; proof of work {record.task_id}; total API estimate ${run.accounted_cost_usd:.6f}"
        )
        tasks = TaskStore(controller.workspace)
        apply_undo(plan_undo(tasks, record.task_id), tasks)
        assert (project / "app.py").read_bytes() == original
        console.print("Undo restored the original; temporary demo project will be removed.")
        return run


if __name__ == "__main__":
    asyncio.run(run_demo())
