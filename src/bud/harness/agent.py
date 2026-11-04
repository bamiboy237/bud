"""Builds bud's agent with its system prompt, guarded tools, and persistent limits."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from importlib import resources
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.capabilities import AbstractCapability, Capability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import Memory, SpendLimits
from pydantic_ai_harness.spend import Budget

from bud.gateway.registry import Registry
from bud.harness.actions import GuardTools, Policy, RecordDecision
from bud.harness.memory import MEMORY_RULES
from bud.harness.results import SqliteResultStore, build_output_limits
from bud.harness.spend import SqliteSpendStore

MAX_USD_PER_RUN = Decimal("0.50")

MAX_USD_PER_DAY = Decimal("5.00")

RUN_LIMITS = UsageLimits(request_limit=25, tool_calls_limit=20)


def load_system_prompt() -> str:
    return resources.files("bud.harness").joinpath("prompts/system.md").read_text()


class Harness:
    def __init__(
        self,
        tools: list[Tool] | AbstractToolset[None],
        policy: Policy,
        registry: Registry,
        profile: str,
        *,
        memory: Memory | None = None,
        spend_path: Path,
        result_path: Path,
        read_tools: set[str],
        result_retention: timedelta = timedelta(hours=24),
    ) -> None:
        self.toolset = tools if isinstance(tools, AbstractToolset) else FunctionToolset[None](tools)
        self.memory = memory
        tools_policy = policy.tools | MEMORY_RULES if memory else policy.tools
        self.policy = Policy(
            tools=tools_policy | {"read_tool_result": "allow", "read_tool_result_chars": "allow"}
        )
        self.results = SqliteResultStore(result_path, retention=result_retention)
        self.output_limits = build_output_limits(self.results, read_tools)
        self.registry = registry
        self.system_prompt = f"{load_system_prompt()}\n{profile}"
        memory_guidance = (memory.guidance or "") if memory is not None else ""
        prompt = self.system_prompt + memory_guidance
        self.prompt_version = sha256(prompt.encode()).hexdigest()[:12]
        self.spend = SpendLimits(
            store=SqliteSpendStore(spend_path),
            budgets=[
                Budget(usd=MAX_USD_PER_RUN, window="run"),
                Budget(usd=MAX_USD_PER_DAY, window="day"),
            ],
            price=self._price,
            on_unpriced="raise",
        )

    def _price(self, response: ModelResponse) -> Decimal | None:
        for spec in self.registry.models:
            if spec.name == response.model_name and spec.provider == response.provider_name:
                if spec.billing == "subscription":
                    # This budget bounds metered API spend, not subscription allowance.
                    return Decimal(0)
                return Decimal(str(spec.cost(response.usage)))
        return None

    def agent(
        self, model: Model, record: RecordDecision, *, durability: AbstractCapability | None = None
    ) -> Agent[None, str | DeferredToolRequests]:
        return Agent(
            model,
            name="bud",
            instructions=self.system_prompt,
            toolsets=[self.toolset],
            output_type=[str, DeferredToolRequests],
            capabilities=[
                GuardTools(policy=self.policy, record=record),
                self.spend,
                self.output_limits,
                Capability(
                    id="bud_result_readback", tools=[Tool(self.results.read_tool_result_chars)]
                ),
                *([self.memory] if self.memory is not None else []),
                *([durability] if durability is not None else []),
            ],
        )
