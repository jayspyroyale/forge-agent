"""Failure boundaries and bounded resource use with real tools and fake models."""

import asyncio
import contextlib
import sqlite3
import sys
import threading
import time

import pytest

from forge.agent.runtime import TaskCancelled, run_task
from forge.agent.state import AgentStatus
from forge.config import ForgeConfig
from forge.concurrency import run_blocking
from forge.memory.store import MemoryStore, MemoryStoreError
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall, Usage
from forge.security.secret_scan import redact_data
from forge.tasks.paths import record_path
from forge.tasks.store import TaskStore
from forge.terminal import BoundedOutput, run_command
from forge.tools.base import Tool, ToolResult
from forge.workspace import Workspace, WorkspaceError


def test_output_memory_is_bounded():
    collector = BoundedOutput(1000)
    for _ in range(1000):
        collector.feed("x" * 10000)
        assert len(collector.head) + len(collector.tail) <= 1000
    text, truncated = collector.result()
    assert truncated and "9999000 characters truncated" in text


def test_real_large_terminal_output(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text("print('start'); print('x' * 2000000); print('end')")
    result = run_command(f'"{sys.executable}" noisy.py', tmp_path, 10, output_limit=1000)
    assert result.succeeded and result.truncated
    assert result.stdout.startswith("start") and result.stdout.endswith("end\n")
    assert len(result.stdout) < 1100


def test_cancel_command_promptly(tmp_path):
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_command(f'"{sys.executable}" -c "import time; time.sleep(30)"', tmp_path, 30, cancel_event=event)
    finally:
        timer.cancel()
    assert not result.succeeded
    assert time.monotonic() - started < 10


def test_cancel_waits_for_worker_before_cleanup(tmp_path):
    started = threading.Event()
    written = tmp_path / "completed"

    def work():
        started.set()
        time.sleep(0.1)
        written.write_text("settled")

    async def scenario():
        task = asyncio.create_task(run_blocking(work))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert written.read_text() == "settled"

    asyncio.run(scenario())


def test_cancelled_task_records_completed_worker_changes(tmp_path):
    started = threading.Event()

    class SlowWrite(Tool):
        name = "slow_write"
        description = "Test write that completes after cancellation"

        def execute(self, args, context):
            target = context.workspace.ensure_writable("result.txt")
            context.notify_before_write(target)
            started.set()
            time.sleep(0.1)
            target.write_text("done")
            return ToolResult.ok()

    from forge.security.permissions import PermissionEngine
    from forge.security.policy import PermissionPolicy

    config = ForgeConfig(workspace=tmp_path, memory={"enabled": False}, agent={"verification": "off"})
    provider = FakeModelProvider(responses=[ModelResponse(tool_calls=[ToolCall(id="slow", name="slow_write")])])

    async def scenario():
        task = asyncio.create_task(
            run_task(
                config,
                "write",
                provider=provider,
                extra_tools=[SlowWrite()],
                permissions=PermissionEngine(PermissionPolicy.permissive()),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(TaskCancelled) as caught:
            await task
        evidence = caught.value.outcome.evidence
        assert evidence.status == "cancelled"
        assert "result.txt" in evidence.files_changed
        assert TaskStore(Workspace(tmp_path)).load_evidence(evidence.task_id).status == "cancelled"

    asyncio.run(scenario())


def test_context_overflow_never_calls_provider(tmp_path):
    config = ForgeConfig(workspace=tmp_path, memory={"enabled": False}, context={"max_tokens": 2000})
    provider = FakeModelProvider(responses=["done"])
    outcome = asyncio.run(run_task(config, "user instruction " * 10000, provider=provider))
    assert outcome.state.status == AgentStatus.FAILED
    assert "Context overflow" in outcome.state.error
    assert not provider.calls


def test_malformed_provider_response_fails_predictably(tmp_path):
    class Malformed(FakeModelProvider):
        async def generate(self, *args, **kwargs):
            return {"tool_calls": "broken"}

    config = ForgeConfig(workspace=tmp_path, memory={"enabled": False})
    outcome = asyncio.run(run_task(config, "task", provider=Malformed()))
    assert outcome.state.status == AgentStatus.FAILED
    assert "Malformed model response" in outcome.state.error


def test_corrupted_memory_is_optional(tmp_path):
    path = tmp_path / "memory.db"
    path.write_bytes(b"invalid sqlite bytes")
    config = ForgeConfig(workspace=tmp_path, memory={"path": path})
    events = []
    outcome = asyncio.run(
        run_task(config, "hello", provider=FakeModelProvider(responses=["done"]), on_event=events.append)
    )
    assert outcome.state.status == AgentStatus.COMPLETED
    assert any("memory is unavailable" in getattr(event, "message", "") for event in events)


def test_corrupted_memory_row_is_a_store_error(tmp_path):
    path = tmp_path / "memory.db"
    from forge.memory.models import MemoryInput

    with MemoryStore(path) as store:
        record = store.remember("project", MemoryInput(content="uses Python", source="test", kind="fact")).record
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE memories SET created_at = 'invalid' WHERE id = ?", (record.id,))
    with MemoryStore(path) as store, pytest.raises(MemoryStoreError, match="Invalid memory record"):
        store.list_memories("project")


@pytest.mark.parametrize("relative", ["../outside", "../project/inside"])
def test_record_parent_traversal_is_refused(tmp_path, relative):
    with pytest.raises(WorkspaceError):
        record_path(tmp_path, relative)


def test_record_symlink_is_refused(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    try:
        (tmp_path / ".forge").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    with pytest.raises(WorkspaceError):
        TaskStore(Workspace(tmp_path))


def test_usage_rejects_fabricated_negative_or_infinite_values():
    from pydantic import ValidationError

    for data in ({"input_tokens": -1}, {"cost_usd": float("inf")}, {"cost_usd": -1}):
        with pytest.raises(ValidationError):
            Usage(**data)


def test_metadata_redacts_sensitive_values():
    secret = "ghp_" + "a" * 36
    result = redact_data({"api_key": "private value", "message": secret, "tokens": 12})
    assert "private value" not in str(result) and secret not in str(result)
    assert result["tokens"] == 12


def test_interrupted_exploration_keeps_evidence_and_cleans_worktrees(calculator_project, tmp_path):
    from exploration_helpers import (
        BUG,
        FIX,
        Scripts,
        edit,
        explore_config,
        init_repo,
        permissive,
        plans_reply,
        tree_hashes,
    )
    from forge.exploration.controller import ExplorationController

    project = init_repo(calculator_project)
    before = tree_hashes(project)
    scripts = Scripts(project, {"planner": [plans_reply("Direct", "Alternative")], "A": [edit(BUG, FIX)]})

    async def scenario():
        ready = asyncio.Event()

        def factory(config):
            provider = scripts(config)
            generate = provider.generate
            if config.workspace_root.name == "A":

                async def paused(*args, **kwargs):
                    if provider.calls:
                        ready.set()
                        await asyncio.Event().wait()
                    return await generate(*args, **kwargs)

                provider.generate = paused
            return provider

        controller = ExplorationController(
            explore_config(project, tmp_path), provider_factory=factory, permissions=permissive()
        )
        task = asyncio.create_task(controller.explore("Fix multiply"))
        await asyncio.wait_for(ready.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        run = controller.store.latest()
        assert run.status == "cancelled"
        assert run.candidate("A").status == "cancelled"
        assert run.candidate("A").evidence is not None
        assert run.candidate("A").files
        assert controller.store.candidate_file(run.run_id, "A", "calculator.py")
        assert not (controller.work_dir / run.run_id).exists()
        assert tree_hashes(project) == before

    asyncio.run(scenario())
