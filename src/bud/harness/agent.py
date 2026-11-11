"""Builds bud's agent with its system prompt, guarded tools, and persistent limits."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from importlib import resources
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability, Capability
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import Memory, SpendLimits
from pydantic_ai_harness.spend import Budget

from bud.gateway.registry import Registry
from bud.harness.actions import GuardTools, Policy, RecordDecision
from bud.harness.code import CODE_GUIDANCE, CODE_TOOL_CALL_LIMIT, build_code_mode
from bud.harness.context import BackgroundCompaction
from bud.harness.memory import MEMORY_RULES
from bud.harness.results import SqliteResultStore, build_output_limits
from bud.harness.spend import SqliteSpendStore
from bud.limits import MAX_MODEL_REQUESTS_PER_RUN, MAX_TOOL_CALLS_PER_RUN
from bud.skills import GUIDANCE as SKILL_GUIDANCE
from bud.skills import RULES as SKILL_RULES
from bud.skills import SkillMethods, SkillStore

MAX_USD_PER_RUN = Decimal("0.50")

MAX_USD_PER_DAY = Decimal("5.00")

RUN_LIMITS = UsageLimits(
    request_limit=MAX_MODEL_REQUESTS_PER_RUN,
    tool_calls_limit=MAX_TOOL_CALLS_PER_RUN,
)


def load_system_prompt() -> str:
    return resources.files("bud.harness").joinpath("prompts/system.md").read_text()


class Harness:
    def __init__(
        self,
        tools: list[Tool[None]] | AbstractToolset[None],
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
        self.skills = SkillStore(spend_path.parent / "skills")
        self.toolset = CombinedToolset([self.toolset, FunctionToolset[None](self.skills.tools())])
        self.memory = memory
        tools_policy = policy.tools | MEMORY_RULES if memory else policy.tools
        self.policy = Policy(
            tools={"load_tool_result": "allow", "run_code": "allow"}
            | tools_policy
            | {"read_tool_result": "allow", "read_tool_result_chars": "allow"}
            | SKILL_RULES
        )
        self.results = SqliteResultStore(result_path, retention=result_retention)
        self.output_limits = build_output_limits(self.results, read_tools)
        self.registry = registry
        self.code_mode = build_code_mode(
            {
                name
                for name in read_tools
                | {
                    "read_memory",
                    "search_memory",
                    "read_tool_result",
                    "read_tool_result_chars",
                    "load_tool_result",
                }
                if self.policy.decide(name) == "allow"
            }
        )
        self.system_prompt = f"{load_system_prompt()}\n{profile}\n{CODE_GUIDANCE}\n{SKILL_GUIDANCE}"
        # Instructions that must reflect live state (e.g. the approval mode) are
        # evaluated at the start of every run instead of being baked in at boot.
        self.live_instructions: list[Callable[..., str]] = []
        memory_guidance = (memory.guidance or "") if memory is not None else ""
        prompt = self.system_prompt + memory_guidance
        self.prompt_version = sha256(prompt.encode()).hexdigest()[:12]
        self.spend_store = SqliteSpendStore(spend_path)
        self.spend = self.spend_for_run()

    def spend_for_run(self, run_id: str | None = None) -> SpendLimits:
        return SpendLimits(
            store=self.spend_store,
            budgets=[
                Budget(
                    usd=MAX_USD_PER_RUN,
                    window="total",
                    name="execution",
                    scope=lambda ctx: run_id if run_id is not None else str(ctx.run_id),
                    retain="forever",
                ),
                Budget(usd=MAX_USD_PER_DAY, window="day"),
            ],
            price=self._price,
            on_unpriced="raise",
        )

    def _price(self, response: ModelResponse) -> Decimal | None:
        route = (response.metadata or {}).get("bud_model_id")
        for spec in self.registry.models:
            if spec.id == route or (
                route is None
                and spec.name == response.model_name
                and spec.provider == response.provider_name
            ):
                if spec.billing == "subscription":
                    # This budget bounds metered API spend, not subscription allowance.
                    return Decimal(0)
                return Decimal(str(spec.cost(response.usage)))
        return None

    def agent(
        self,
        model: Model,
        record: RecordDecision,
        *,
        context: BackgroundCompaction,
        durability: AbstractCapability | None = None,
        capabilities: tuple[AbstractCapability[None], ...] = (),
    ) -> Agent[None, str | DeferredToolRequests]:
        return Agent(
            model,
            name="bud",
            deps_type=type(None),
            instructions=[self.system_prompt, *self.live_instructions],
            toolsets=[self.toolset],
            output_type=[str, DeferredToolRequests],
            capabilities=[
                GuardTools(policy=self.policy, record=record),
                self.spend,
                self.output_limits,
                self.code_mode,
                context,
                ExecutionBudget(),
                SkillMethods(store=self.skills),
                Capability(
                    id="bud_result_readback",
                    tools=[
                        Tool(self.results.read_tool_result_chars),
                        Tool(self.results.load_tool_result),
                    ],
                ),
                *([self.memory] if self.memory is not None else []),
                *([durability] if durability is not None else []),
                *capabilities,
            ],
        )


@dataclass(kw_only=True)
class ExecutionBudget(AbstractCapability[None]):
    id: str = "bud_execution_budget"

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        limit = ctx.usage_limits or RUN_LIMITS
        tool_limit = limit.tool_calls_limit
        request_limit = limit.request_limit
        tools = max(
            0,
            (RUN_LIMITS.tool_calls_limit if tool_limit is None else tool_limit)
            - ctx.usage.tool_calls,
        )
        requests = max(
            0,
            (RUN_LIMITS.request_limit if request_limit is None else request_limit)
            - ctx.usage.requests,
        )
        finish = tools <= 3 or requests <= 1
        guidance = (
            f"Remaining execution budget: {tools} tool calls and {requests} model requests. "
            f"A run_code snippet can make at most "
            f"{min(CODE_TOOL_CALL_LIMIT, max(0, tools - 1))} nested calls "
            "within that remaining budget. Each nested call and run_code itself count. "
            "When three or fewer tool calls remain, stop optional research and use the "
            "evidence already gathered to deliver; state any material coverage gap. "
            "Reuse fetched results and investigate only decision-relevant gaps. "
            "Reserve needed calls for saving or validating the requested result."
        )
        if finish:
            guidance += (
                " Tools are now disabled. Deliver the verified result and identify any "
                "unfinished work or missing evidence; do not claim an action succeeded."
            )
        # A per-request note, not an instruction part: it must not change the system
        # prefix, or strict endpoints reject the request and provider prompt caching
        # misses on every turn. request_context.messages is request-owned, so this
        # note reaches only the current model request.
        budget_note = ModelRequest(parts=[UserPromptPart(content=guidance)])
        return replace(
            request_context,
            messages=[*request_context.messages, budget_note],
            model_settings=(
                (request_context.model_settings or {}) | {"tool_choice": "none"}
                if finish
                else request_context.model_settings
            ),
        )
