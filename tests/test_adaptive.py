"""Deterministic adaptive exploration and shared call-budget tests."""

import asyncio

import pytest
from exploration_helpers import BUG, FIX, Scripts, edit, explore_config, init_repo, permissive, plans_reply

from forge.config import ForgeConfig
from forge.exploration.controller import ExplorationController
from forge.models.budget import BudgetExhausted, BudgetProvider, ExplorationBudget
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import Message, ModelResponse, Usage


def controller(project, tmp_path, scripts, **settings):
    config = explore_config(project, tmp_path, exploration={
        "workspace_dir": str(tmp_path / "worktrees"), "adaptive": True, **settings})
    factory = Scripts(project, scripts)
    return ExplorationController(config, permissions=permissive(), provider_factory=factory), factory


def test_strong_candidate_stops_early(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    c, _ = controller(repo, tmp_path, {"planner": [plans_reply("Direct fix", "Other way")],
        "A": [edit(BUG, FIX), "done"], "B": ["nothing"]}, approaches=5)
    run = asyncio.run(c.explore("fix multiply"))
    assert len(run.candidates) == 2
    assert run.stop_reason == "strong verified candidate dominates"
    assert BUG in (repo / "calculator.py").read_text()


def test_close_results_generate_another_candidate_and_lineage(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    c, factory = controller(repo, tmp_path, {"planner": [plans_reply("Direct repair", "Algebraic repair")],
        "planner2": [plans_reply("Independent product")],
        "A": [edit(BUG, FIX), "done"], "B": [edit(BUG, FIX), "done"],
        "C": [edit(BUG, FIX), "done"]}, approaches=3, dominance_margin=1,
        experimental_generation=True)
    run = asyncio.run(c.explore("fix multiply"))
    assert len(run.candidates) == 3
    assert run.plans[2].generation == 2
    assert set(run.plans[2].parents) == {"A", "B"}
    text = factory.providers["planner2"].calls[0]["messages"][1].content
    assert "measured evidence" in text and "Direct repair" in text
    assert run.stop_reason == "maximum candidates reached"


def test_model_pool_round_robin_and_same_approach(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    c, factory = controller(repo, tmp_path, {"planner": [plans_reply("Direct repair")],
        "A": [edit(BUG, FIX), "done"], "B": [edit(BUG, FIX), "done"]},
        approaches=2, strategy="same_approach", model_pool=[
            {"provider": "fake", "name": "small"}, {"provider": "fake", "name": "large"}])
    run = asyncio.run(c.explore("fix multiply"))
    assert run.plans[0].summary == run.plans[1].summary
    assert [c.model for c in run.candidates] == ["small", "large"]
    assert factory.configs["B"].model.name == "large"


def test_explicit_assignment_overrides_pool(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    c, factory = controller(repo, tmp_path, {"planner": [plans_reply("Direct repair", "Other way")],
        "A": ["nothing"], "B": ["nothing"]}, approaches=2,
        model_assignment="explicit", candidate_models={"B": {"provider": "fake", "name": "chosen"}})
    asyncio.run(c.explore("fix multiply"))
    assert factory.configs["B"].model.name == "chosen"


def test_budget_stops_before_first_paid_call(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    c, factory = controller(repo, tmp_path, {"planner": [plans_reply("Direct repair")]}, max_tokens=1)
    run = asyncio.run(c.explore("fix multiply"))
    assert run.status == "failed" and run.candidates == []
    assert run.model_calls == 0 and "token budget" in run.stop_reason
    assert factory.providers["planner"].calls == []


def test_cost_budget_refuses_unknown_pricing(calculator_project, tmp_path):
    c, factory = controller(calculator_project, tmp_path, {}, max_api_cost=0.1)
    run = asyncio.run(c.explore("fix multiply"))
    assert "requires configured" in run.stop_reason
    assert factory.providers["planner"].calls == []


def test_shared_budget_prices_usage_and_accounts_for_every_call():
    config = ForgeConfig(model={"provider": "fake", "input_cost_per_million": 2,
        "output_cost_per_million": 4, "max_response_tokens": 20})
    ledger = ExplorationBudget(max_tokens=10000, max_cost=1)
    p = BudgetProvider(FakeModelProvider(config, responses=[
        ModelResponse(content="ok", usage=Usage(input_tokens=100, output_tokens=20)),
        ModelResponse(content="ok", usage=Usage(input_tokens=200, output_tokens=10))]), ledger)
    asyncio.run(p.generate([Message.user("hello")]))
    asyncio.run(p.generate([Message.user("hello")]))
    assert ledger.tokens == 330 and ledger.calls == 2
    assert ledger.cost == pytest.approx(0.00072)
    assert not ledger.unknown_cost


def test_missing_usage_retains_conservative_reservation():
    p = FakeModelProvider(responses=["ok"])
    ledger = ExplorationBudget(max_tokens=10000)
    asyncio.run(BudgetProvider(p, ledger).generate([Message.user("hello")]))
    assert ledger.estimated_calls == 1 and ledger.tokens > 4096
    assert ledger.unknown_cost


def test_deadline_cancels_hung_provider():
    class Hung(FakeModelProvider):
        async def generate(self, *args, **kwargs):
            await asyncio.sleep(10)
    ledger = ExplorationBudget(max_seconds=0.01)
    with pytest.raises(BudgetExhausted, match="elapsed time"):
        asyncio.run(BudgetProvider(Hung(), ledger).generate([Message.user("hello")]))
    assert ledger.calls == 1


def test_plateau_stops_repeated_weak_results(calculator_project, tmp_path):
    c, _ = controller(calculator_project, tmp_path, {
        "planner": [plans_reply("Direct repair", "Algebraic repair")],
        "planner2": [plans_reply("Independent product")], "A": ["nothing"],
        "B": ["nothing"], "C": ["nothing"]}, approaches=5, plateau_rounds=1)
    run = asyncio.run(c.explore("fix multiply"))
    assert len(run.candidates) == 3 and run.stop_reason == "improvement plateau reached"
