"""Builds the agent bud runs: its system prompt, guarded tools, and limits.
The core calls `Harness.agent` once per run, so each run gets a gateway that records to its
own session. Spend counters live on the `Harness` and carry across runs.
"""

from decimal import Decimal
from hashlib import sha256
from importlib import resources

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import SpendLimits
from pydantic_ai_harness.spend import Budget

from bud.gateway.registry import Registry
from bud.harness.actions import Policy, RecordDecision, guard
from bud.harness.tools import Tool

MAX_USD_PER_RUN = Decimal("0.50")
# Counted in memory, so a server restart resets the day's total.
MAX_USD_PER_DAY = Decimal("5.00")

RUN_LIMITS = UsageLimits(request_limit=25, tool_calls_limit=20)


def load_system_prompt() -> str:
    return resources.files("bud.harness").joinpath("prompts/system.md").read_text()


class Harness:
    def __init__(self, tools: list[Tool], policy: Policy, registry: Registry) -> None:
        self.toolset = FunctionToolset[None](tools)
        self.policy = policy
        self.registry = registry
        self.system_prompt = load_system_prompt()
        self.prompt_version = sha256(self.system_prompt.encode()).hexdigest()[:12]
        self.spend = SpendLimits(
            budgets=[
                Budget(usd=MAX_USD_PER_RUN, window="run"),
                Budget(usd=MAX_USD_PER_DAY, window="day"),
            ],
            price=self._price,
            on_unpriced="raise",
        )

    def _price(self, response: ModelResponse) -> Decimal | None:
        for spec in self.registry.models:
            if spec.name == response.model_name:
                return Decimal(str(spec.cost(response.usage)))
        return None

    def agent(
        self, model: Model, record: RecordDecision
    ) -> Agent[None, str | DeferredToolRequests]:
        return Agent(
            model,
            instructions=self.system_prompt,
            toolsets=[guard(self.toolset, self.policy, record)],
            output_type=[str, DeferredToolRequests],
            capabilities=[self.spend],
        )
