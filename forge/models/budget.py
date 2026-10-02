"""Shared call admission and accounting, independent of the provider implementation.

Reserve a conservative UTF-8 input size plus the output cap before a call.
Reported usage replaces the reservation; missing usage retains it as an
estimate. Prices are supplied by the user, never guessed from model names.
This is an admission budget, not a guarantee about a vendor's billing/tokenizer.
"""

import asyncio
import json
import time

from forge.config.schema import ModelSettings
from forge.models.base import ModelProvider
from forge.models.errors import ModelError
from forge.models.types import Usage


class BudgetExhausted(ModelError):
    pass


class ExplorationBudget:
    def __init__(self, *, max_tokens=None, max_cost=None, max_seconds=None):
        self.max_tokens = max_tokens
        self.max_cost = max_cost
        self.deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        self.tokens = 0
        self.cost = 0.0
        self.calls = 0
        self.estimated_calls = 0
        self.stop_reason = None
        self.unknown_cost = False

    def check(self):
        reason = self.stop_reason
        if self.deadline is not None and time.monotonic() >= self.deadline:
            reason = "elapsed time budget reached"
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            reason = "token budget reached"
        if self.max_cost is not None and self.cost >= self.max_cost:
            reason = "API cost budget reached"
        if reason:
            self.stop_reason = reason
            raise BudgetExhausted(reason)

    def reserve(self, tokens, cost):
        self.check()
        if self.max_cost is not None and cost is None:
            self.stop_reason = "cost budget requires configured input and output prices"
        elif self.max_tokens is not None and self.tokens + tokens > self.max_tokens:
            self.stop_reason = "next model call would exceed token budget"
        elif self.max_cost is not None and self.cost + cost > self.max_cost:
            self.stop_reason = "next model call would exceed API cost budget"
        self.check()


def priced_usage(usage: Usage, settings: ModelSettings) -> Usage:
    if usage.cost_usd is not None:
        return usage
    if settings.input_cost_per_million is None or settings.output_cost_per_million is None:
        return usage
    return usage.model_copy(update={"cost_usd": (
        usage.input_tokens * settings.input_cost_per_million
        + usage.output_tokens * settings.output_cost_per_million
    ) / 1_000_000})


class BudgetProvider(ModelProvider):
    """Wrap the ordinary provider; planning, implementation and review share one ledger."""
    def __init__(self, provider, budget):
        super().__init__(provider.config)
        self.provider = provider
        self.budget = budget
        self.name = provider.name
        self.default_model = provider.default_model

    async def generate(self, messages, tools=None, **options):
        settings = self.config.model
        cap = settings.max_response_tokens
        wire = json.dumps([m.model_dump() for m in messages], ensure_ascii=False)
        wire += json.dumps([t.model_dump() for t in tools or []], ensure_ascii=False)
        input_bound = len(wire.encode("utf-8")) + 256
        reserve = priced_usage(Usage(input_tokens=input_bound, output_tokens=cap), settings)
        ledger = self.budget
        ledger.reserve(reserve.total_tokens, reserve.cost_usd)
        ledger.calls += 1
        # Charge the reservation on failures too: the vendor may have processed the request.
        ledger.tokens += reserve.total_tokens
        ledger.cost += reserve.cost_usd or 0
        try:
            remaining = settings.timeout
            if ledger.deadline is not None:
                remaining = min(remaining, max(0.001, ledger.deadline - time.monotonic()))
            response = await asyncio.wait_for(
                self.provider.generate(messages, tools=tools, **options), timeout=remaining)
        except TimeoutError as error:
            ledger.unknown_cost |= reserve.cost_usd is None
            ledger.stop_reason = "model timeout or elapsed time budget reached"
            raise BudgetExhausted(ledger.stop_reason) from error
        except BaseException:
            ledger.unknown_cost |= reserve.cost_usd is None
            raise
        if response.usage is None:
            ledger.estimated_calls += 1
            ledger.unknown_cost |= reserve.cost_usd is None
        else:
            usage = priced_usage(response.usage, settings)
            response = response.model_copy(update={"usage": usage})
            ledger.tokens += usage.total_tokens - reserve.total_tokens
            ledger.cost += (usage.cost_usd or 0) - (reserve.cost_usd or 0)
            ledger.unknown_cost |= usage.cost_usd is None
        return response
