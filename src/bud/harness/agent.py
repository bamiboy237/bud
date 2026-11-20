"""Builds bud's agent with its system prompt, guarded tools, and spiral detection."""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.capabilities import (
    AbstractCapability,
    Caching,
    Capability,
    RaiseContentFilterError,
)
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset
from pydantic_ai_harness import Memory, SystemReminders, WarnOnCacheBusts
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.system_reminders import Reminder

from bud.config import get_settings
from bud.gateway.registry import Registry
from bud.gateway.spend import build_spend_limits
from bud.harness.actions import Action, GuardTools, Policy, RecordDecision
from bud.harness.actions import _pinned as pinned_account
from bud.harness.code import CODE_GUIDANCE, build_code_mode
from bud.harness.context import BackgroundCompaction
from bud.harness.live import LiveState, LiveTools
from bud.harness.memory import MEMORY_RULES
from bud.harness.results import SqliteResultStore, build_output_limits
from bud.harness.review import Describer, SessionGrants, ToolReviewer
from bud.harness.spiral import (
    LoopReport,
    SpiralConfig,
    SpiralGuard,
    build_judge,
    loop_detector,
)
from bud.skills import GUIDANCE as SKILL_GUIDANCE
from bud.skills import RULES as SKILL_RULES
from bud.skills import SkillMethods, SkillStore

LONG_RUN_REMINDER_EVERY = 8
LONG_RUN_REMINDER = (
    "Check your next step still serves the owner's most recent request. Use the results "
    "you already have instead of calling a tool again for the same thing. Writes still go "
    "through approval: propose them, never work around a denial."
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
        state_dir: Path,
        result_path: Path,
        read_tools: set[str],
        result_retention: timedelta = timedelta(hours=24),
        capabilities: Sequence[AbstractCapability[None]] = (),
    ) -> None:
        # Fixed for the process, unlike `tools`, which swap_tools replaces between runs.
        self.capabilities = list(capabilities)
        self.toolset = tools if isinstance(tools, AbstractToolset) else FunctionToolset[None](tools)
        self.skills = SkillStore(state_dir / "skills")
        self.toolset = CombinedToolset([self.toolset, FunctionToolset[None](self.skills.tools())])
        # The agent holds this wrapper, so tools can be replaced between runs.
        self.live = LiveTools(state=LiveState(current=self.toolset))
        self.memory = memory
        tools_policy = policy.tools | MEMORY_RULES if memory else policy.tools
        reads: dict[str, Action] = dict.fromkeys(
            ("load_tool_result", "run_code", "search_tools", "search_conversation_history"),
            "allow",
        )
        # It only changes how long Bud's own saved copy of a result lasts.
        reads["keep_tool_result"] = "allow"
        self.policy = Policy(
            tools=reads
            | tools_policy
            | {"read_tool_result": "allow", "read_tool_result_chars": "allow"}
            | SKILL_RULES,
            no_grants=policy.no_grants,
        )
        self.results = SqliteResultStore(result_path, retention=result_retention)
        self.output_limits = build_output_limits(self.results)
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
                    "keep_tool_result",
                }
                if self.policy.decide(name) == "allow"
            }
        )
        self.system_prompt = f"{load_system_prompt()}\n{profile}\n{CODE_GUIDANCE}\n{SKILL_GUIDANCE}"
        # Set by the composition root (bud serve, evals); auto mode then lets it review each
        # write. None means nothing is ever reviewed and auto mode asks like strict.
        self.reviewer: ToolReviewer | None = None
        # What each write's ids point to, rendered from the store: the reviewer's only view
        # of a target that is not spelled out in the call's own arguments.
        self.describers: dict[str, Describer] = {}
        # Per tool: does this call notify people other than the owner? Computed in code.
        self.notifies_others: dict[str, Callable[[dict], bool]] = {}
        # Per tool: is this call harmless, judged by its arguments? It then runs unasked.
        self.harmless: dict[str, Callable[[dict], bool]] = {}
        # Per tool: a reason to refuse the call outright, judged in code, or None.
        self.refuses: dict[str, Callable[[dict], str | None]] = {}
        # Facts stamped on every approval card; see ActionGateway.stamp.
        self.stamp: Callable[[], dict[str, Any]] = pinned_account
        # Calls the owner approved after a hold, per session; see SessionGrants.
        self.grants = SessionGrants()
        # Instructions that must reflect live state (e.g. the approval mode) are
        # evaluated at the start of every run instead of being baked in at boot.
        self.live_instructions: list[Callable[..., str]] = [self._today]
        # The person's clock: real time, or the simulated world's fixed "now".
        self.clock: Callable[[], datetime] = lambda: datetime.now().astimezone()
        memory_guidance = (memory.guidance or "") if memory is not None else ""
        prompt = self.system_prompt + memory_guidance
        self.prompt_version = sha256(prompt.encode()).hexdigest()[:12]
        self.spiral = SpiralConfig.from_settings()
        settings = get_settings()
        # One daily cap for the whole install, so it lives in the data directory, not in
        # this account's session folder.
        self.spend = build_spend_limits(
            settings.data_dir / "spend.db", registry, settings.daily_spend_usd
        )

    def swap_tools(self, toolset: AbstractToolset[None]) -> None:
        """Replace the tools the next run sees; the running step keeps the ones it started with."""
        self.live.swap(CombinedToolset([toolset, FunctionToolset[None](self.skills.tools())]))

    def _today(self) -> str:
        # Day granularity: instructions lead the prompt, so a per-minute clock here
        # would invalidate the cached conversation on every turn.
        now = self.clock()
        return (
            f"\nToday is {now:%A, %d %B %Y} (UTC{now:%z}). "
            "Call current_time when the exact time matters."
        )

    def agent(
        self,
        model: Model,
        record: RecordDecision,
        *,
        context: BackgroundCompaction,
        loop_report: LoopReport | None = None,
        durability: AbstractCapability | None = None,
        capabilities: tuple[AbstractCapability[None], ...] = (),
    ) -> Agent[None, str | DeferredToolRequests]:
        return Agent(
            model,
            name="bud",
            deps_type=type(None),
            instructions=[self.system_prompt, *self.live_instructions],
            toolsets=[self.live],
            output_type=[str, DeferredToolRequests],
            capabilities=[
                RepairToolArguments(),
                GuardTools(
                    policy=self.policy,
                    record=record,
                    reviewer=self.reviewer,
                    describers=self.describers,
                    notifies_others=self.notifies_others,
                    harmless=self.harmless,
                    refuses=self.refuses,
                    stamp=lambda: self.stamp(),
                    facts=self._today,
                    grants=self.grants,
                ),
                self.output_limits,
                self.code_mode,
                context,
                *(
                    [loop_detector(self.spiral, loop_report or (lambda _: None))]
                    if self.spiral.enabled
                    else []
                ),
                SpiralGuard(
                    config=self.spiral,
                    report=loop_report or (lambda _: None),
                    judge=build_judge(self.spiral),
                ),
                SkillMethods(store=self.skills),
                Capability(
                    id="bud_result_readback",
                    tools=[
                        Tool(self.results.read_tool_result_chars),
                        Tool(self.results.load_tool_result),
                        Tool(self.results.keep_tool_result),
                    ],
                ),
                *([self.memory] if self.memory is not None else []),
                *([self.spend] if self.spend is not None else []),
                Caching(),
                WarnOnCacheBusts(),
                RaiseContentFilterError(),
                # Request-only reminders leave the cached history unchanged.
                SystemReminders(
                    reminders=[
                        Reminder(LONG_RUN_REMINDER, interval=LONG_RUN_REMINDER_EVERY, tag=None)
                    ]
                ),
                *([durability] if durability is not None else []),
                *self.capabilities,
                *capabilities,
            ],
        )
