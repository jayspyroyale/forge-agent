"""Persistent project memory: storage, scoping, trust, conflicts, expiry, retrieval, CLI, agent use."""

import asyncio
import json
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from forge.agent.runtime import memory_path, run_task
from forge.cli import app
from forge.config import ForgeConfig
from forge.memory import MemoryInput, MemoryNotFoundError, MemorySecretError, MemoryStore, MemoryStoreError, project_key
from forge.memory.facts import detect_project_facts
from forge.memory.models import now
from forge.memory.retrieval import relevant_memories
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "memory.db"


def fact(content, subject="package_manager:node", source="file:package.json", confidence="high", verification="verified"):
    return MemoryInput(kind="fact", subject=subject, content=content, source=source, confidence=confidence, verification=verification)


# --- Persistence and scoping ----------------------------------------------------------------


def test_memories_persist_across_sessions(db):
    with MemoryStore(db) as store:
        created = store.remember("/proj", fact("Uses pnpm")).record
    with MemoryStore(db) as store:  # a new session
        (record,) = store.list_memories("/proj")
    assert record.id == created.id
    assert record.content == "Uses pnpm"
    assert record.source == "file:package.json"


def test_memories_are_scoped_to_their_project(db):
    with MemoryStore(db) as store:
        store.remember("/a", fact("Uses pnpm"))
        store.remember("/b", fact("Uses yarn"))
        assert [r.content for r in store.list_memories("/a")] == ["Uses pnpm"]
        assert [r.content for r in store.list_memories("/b")] == ["Uses yarn"]
        with pytest.raises(MemoryNotFoundError):
            store.get(store.list_memories("/a")[0].id, project="/b")


def test_project_key_is_the_resolved_root(tmp_path):
    assert project_key(tmp_path / "x" / "..") == tmp_path.resolve().as_posix()


# --- Confidence and conflicts ---------------------------------------------------------------


def test_same_memory_is_refreshed_not_duplicated(db):
    with MemoryStore(db) as store:
        first = store.remember("/p", MemoryInput(kind="preference", content="Prefer REST", confidence="low", source="conversation"))
        again = store.remember("/p", MemoryInput(kind="preference", content="prefer  rest", confidence="high", source="user"))
        assert again.action == "refreshed"
        (record,) = store.list_memories("/p")
    assert record.id == first.record.id
    assert (record.confidence, record.source) == ("high", "user")


def test_newer_evidence_supersedes_older_memory(db):
    with MemoryStore(db) as store:
        old = store.remember("/p", fact("Uses npm", source="file:package-lock.json")).record
        result = store.remember("/p", fact("Uses pnpm", source="file:pnpm-lock.yaml"))
        assert result.action == "superseded"
        assert result.replaced == [old.id]
        assert [r.content for r in store.list_memories("/p")] == ["Uses pnpm"]
        retired = store.get(old.id)
    assert retired.status == "superseded"
    assert retired.superseded_by == result.record.id


def test_weaker_contradiction_is_contested_not_trusted(db):
    with MemoryStore(db) as store:
        verified = store.remember("/p", fact("Uses pnpm")).record
        claim = store.remember("/p", fact("Uses yarn", source="model", confidence="low", verification="unverified"))
        assert claim.action == "contested"
        assert claim.record.conflicts_with == verified.id
        assert [r.content for r in store.list_memories("/p")] == ["Uses pnpm"]
        statuses = {r.content: r.status for r in store.list_memories("/p", include_inactive=True)}
    assert statuses == {"Uses pnpm": "active", "Uses yarn": "contested"}


def test_memories_without_subject_do_not_replace_each_other(db):
    with MemoryStore(db) as store:
        store.remember("/p", MemoryInput(kind="decision", content="Use SQLite for the cache"))
        store.remember("/p", MemoryInput(kind="decision", content="Keep the API synchronous"))
        assert len(store.list_memories("/p")) == 2


# --- Expiration -----------------------------------------------------------------------------


def test_expired_memories_are_not_used_and_can_be_pruned(db):
    with MemoryStore(db) as store:
        store.remember("/p", MemoryInput(kind="workflow", content="Skip test_slow until the fix lands", expires_at=now() - timedelta(seconds=1)))
        store.remember("/p", MemoryInput(kind="architecture", content="Hexagonal architecture"))
        assert [r.content for r in store.list_memories("/p")] == ["Hexagonal architecture"]
        assert len(store.list_memories("/p", include_inactive=True)) == 2
        assert store.prune_expired("/p") == 1
        assert len(store.list_memories("/p", include_inactive=True)) == 1


def test_future_expiry_is_still_usable(db):
    with MemoryStore(db) as store:
        store.remember("/p", MemoryInput(kind="workflow", content="Workaround active", expires_at=now() + timedelta(days=7)))
        assert len(store.list_memories("/p")) == 1


# --- Safety and failures -----------------------------------------------------------------------


@pytest.mark.parametrize("content", ["API_KEY=sk-abcdefghijklmnopqrstuv", "token ghp_abcdefghijklmnopqrstuvwxyz123", "password: hunter2hunter2"])
def test_secrets_are_refused(db, content):
    with MemoryStore(db) as store:
        with pytest.raises(MemorySecretError):
            store.remember("/p", MemoryInput(kind="fact", content=content))
        assert store.list_memories("/p", include_inactive=True) == []


def test_corrupted_database_is_a_clear_error(tmp_path):
    broken = tmp_path / "memory.db"
    broken.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(MemoryStoreError, match="Cannot open the memory database"):
        MemoryStore(broken)


def test_deletion(db):
    with MemoryStore(db) as store:
        record = store.remember("/p", fact("Uses pnpm")).record
        assert store.forget(record.id, "/other") is False
        assert store.forget(record.id, "/p") is True
        assert store.forget(record.id, "/p") is False
        assert store.list_memories("/p", include_inactive=True) == []


# --- Facts and retrieval -----------------------------------------------------------------------


def test_project_facts_come_from_files(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "tsconfig.json").write_text("{}")
    facts = {f.subject: f for f in detect_project_facts(tmp_path)}
    assert facts["package_manager:node"].content == "Uses pnpm to manage node dependencies"
    assert facts["package_manager:node"].source == "file:pnpm-lock.yaml"
    assert facts["package_manager:node"].confidence == "high"
    assert facts["languages"].content == "Main languages: TypeScript"


def test_retrieval_is_selective(db):
    with MemoryStore(db) as store:
        store.remember("/p", MemoryInput(kind="instruction", content="Always run tests with -x"))
        store.remember("/p", MemoryInput(kind="decision", content="Cache layer uses SQLite"))
        store.remember("/p", MemoryInput(kind="decision", content="Billing goes through Stripe"))
        store.remember("/p", MemoryInput(kind="preference", content="Maybe prefers tabs", confidence="low", source="model"))
        records = store.list_memories("/p")

    chosen = [r.content for r in relevant_memories(records, "Improve the cache eviction")]
    assert chosen == ["Cache layer uses SQLite", "Always run tests with -x"]
    assert relevant_memories(records, "anything", limit=1)[0].content == "Always run tests with -x"


# --- Agent integration --------------------------------------------------------------------------


def run(project, responses, task="Improve the cache", **config):
    provider = FakeModelProvider(responses=responses)
    outcome = asyncio.run(
        run_task(
            ForgeConfig(workspace=project, verification="off", **config),
            task,
            provider=provider,
            permissions=PermissionEngine(PermissionPolicy.permissive()),
        )
    )
    return outcome, provider


def test_relevant_memory_reaches_the_model_with_provenance(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "uv.lock").write_text("")
    config = ForgeConfig(workspace=project)
    with MemoryStore(memory_path(config)) as store:
        decision = store.remember(project_key(project), MemoryInput(kind="decision", content="The cache layer uses SQLite")).record
        store.remember(project_key(project), MemoryInput(kind="decision", content="Billing goes through Stripe"))

    outcome, provider = run(project, ["Done."])

    sent = "\n".join(m.content for m in provider.calls[0]["messages"])
    assert "[decision; medium confidence; from user] The cache layer uses SQLite" in sent
    assert "Uses uv to manage python dependencies" in sent  # fact detected from uv.lock this run
    assert "Stripe" not in sent
    assert decision.id in outcome.evidence.extra["memories_used"]


def test_memory_can_be_disabled(tmp_path):
    outcome, provider = run(tmp_path, ["Done."], memory={"enabled": False})
    assert "[Forge memory]" not in "\n".join(m.content for m in provider.calls[0]["messages"])
    assert not memory_path(ForgeConfig()).exists()


def test_broken_memory_does_not_stop_the_task(tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"garbage" * 500)
    events = []
    provider = FakeModelProvider(responses=["Done."])
    outcome = asyncio.run(
        run_task(
            ForgeConfig(workspace=tmp_path, memory={"path": str(broken)}),
            "task",
            provider=provider,
            on_event=events.append,
        )
    )
    assert outcome.state.status == "completed"
    assert any("Project memory is unavailable" in getattr(event, "message", "") for event in events)


def test_model_writes_are_off_by_default_and_low_confidence_when_on(tmp_path):
    remember = ModelResponse(
        tool_calls=[ToolCall(id="m1", name="remember", arguments={"kind": "workflow", "content": "Run make check before committing"})]
    )
    _, provider = run(tmp_path, ["Done."])
    assert "remember" not in [tool.name for tool in provider.calls[0]["tools"]]

    outcome, _ = run(tmp_path, [remember, "Done."], memory={"model_writes": True})
    assert outcome.state.tool_history[0].result.success
    with MemoryStore(memory_path(ForgeConfig())) as store:
        (record,) = [r for r in store.list_memories(project_key(tmp_path)) if r.kind == "workflow"]
    assert (record.source, record.confidence, record.verification) == ("model", "low", "unverified")


# --- CLI ------------------------------------------------------------------------------------------


def test_memory_cli_add_list_inspect_forget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    added = runner.invoke(app, ["memory", "add", "instruction", "Always run tests with -x"])
    assert added.exit_code == 0, added.output
    assert "Created memory" in added.output

    listed = runner.invoke(app, ["memory", "list", "--json"])
    (record,) = json.loads(listed.output)
    assert (record["kind"], record["source"], record["confidence"]) == ("instruction", "user", "high")

    inspected = runner.invoke(app, ["memory", "inspect", record["id"]])
    assert "Always run tests with -x" in inspected.output

    forgotten = runner.invoke(app, ["memory", "forget", record["id"], "--yes"])
    assert "Forgot" in forgotten.output
    assert json.loads(runner.invoke(app, ["memory", "list", "--json"]).output) == []


def test_memory_cli_refuses_secrets_and_bad_kinds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret = runner.invoke(app, ["memory", "add", "fact", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"])
    assert secret.exit_code == 1
    assert "sk-abcdefghijklmnopqrstuvwx" not in secret.output
    bad = runner.invoke(app, ["memory", "add", "gossip", "x"])
    assert bad.exit_code == 1 and "Invalid memory" in bad.output


def test_memory_cli_unknown_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["memory", "forget", "nope", "--yes"])
    assert result.exit_code == 1 and "No memory with id 'nope'" in result.output
