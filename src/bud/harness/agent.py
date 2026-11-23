"""Builds bud's agent with its system prompt, guarded tools, and spiral detection."""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, AgentRetries, DeferredToolRequests, Tool
from pydantic_ai.capabilities import (
    AbstractCapability,
    Caching,
    Capability,
    RaiseContentFilterError,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import Memory, SystemReminders, WarnOnCacheBusts
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.system_reminders import Reminder

from bud.config import get_settings
from bud.gateway.registry import Registry
from bud.gateway.spend import build_spend_limits
from bud.harness.actions import Action, GuardTools, Policy, RecordDecision
from bud.harness.actions import _pinned as pinned_account
from bud.harness.code import CODE_GUIDANCE, ReadOnlyCodeMode, build_code_mode
from bud.harness.context import BackgroundCompaction
from bud.harness.live import LiveState, LiveTools
from bud.harness.memory import MEMORY_RULES
from bud.harness.operating_profile import BASELINE_PROFILE, OperatingProfile
from bud.harness.preferences import PreferenceStore
from bud.harness.results import SqliteResultStore, build_output_limits
from bud.harness.review import Describer, SessionGrants, ToolReviewer
from bud.harness.spiral import (
    LoopReport,
    SpiralConfig,
    SpiralGuard,
    build_judge,
    loop_detector,
)
from bud.harness.taint import KnownAddresses
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
        preferences: PreferenceStore | None = None,
        state_dir: Path,
        result_path: Path,
        read_tools: set[str],
        result_retention: timedelta | None = None,
        capabilities: Sequence[AbstractCapability[None]] = (),
        operating_profile: OperatingProfile = BASELINE_PROFILE,
    ) -> None:
        self.operating_profile = operating_profile
        harness_cfg = operating_profile.harness
        result_cfg = operating_profile.tool_results
        retention = (
            result_retention
            if result_retention is not None
            else timedelta(hours=result_cfg.retention_hours)
        )
        # Fixed for the process, unlike `tools`, which swap_tools replaces between runs.
        self.capabilities = list(capabilities)
        self.toolset: AbstractToolset[None] = (
            tools if isinstance(tools, AbstractToolset) else FunctionToolset[None](tools)
        )
        self.skills = SkillStore(state_dir / "skills")
        self.memory = memory
        self.preferences = (
            preferences
            if preferences is not None
            else (PreferenceStore(state_dir / "preferences.db", memory=memory) if memory else None)
        )
        toolsets: list[AbstractToolset[None]] = [
            self.toolset,
            FunctionToolset[None](self.skills.tools()),
        ]
        if self.preferences is not None:
            toolsets.append(FunctionToolset[None](self.preferences.tools()))
        self.toolset = CombinedToolset(toolsets)
        # The agent holds this wrapper, so tools can be replaced between runs.
        self.live = LiveTools(state=LiveState(current=self.toolset))
        tools_policy = policy.tools | MEMORY_RULES if memory else policy.tools
        read_names = (
            ("load_tool_result", "run_code", "search_tools", "search_conversation_history")
            if harness_cfg.code_mode_enabled
            else ("load_tool_result", "search_tools", "search_conversation_history")
        )
        reads: dict[str, Action] = dict.fromkeys(read_names, "allow")
        # It only changes how long Bud's own saved copy of a result lasts.
        reads["keep_tool_result"] = "allow"
        self.policy = Policy(
            tools=reads
            | tools_policy
            | {"read_tool_result": "allow", "read_tool_result_chars": "allow"}
            | SKILL_RULES,
            no_grants=policy.no_grants,
        )
        self.results = SqliteResultStore(result_path, retention=retention)
        self.output_limits = build_output_limits(
            self.results,
            spill_chars=result_cfg.spill_chars,
            preview_chars=result_cfg.preview_chars,
            truncate_max_chars=result_cfg.truncate_max_chars,
        )
        self.registry = registry
        self.code_mode: ReadOnlyCodeMode | None = (
            build_code_mode(
                {
                    name
                    for name in read_tools
                    | {
                        "read_memory",
                        "search_memory",
                        "inspect_preferences",
                        "read_tool_result",
                        "read_tool_result_chars",
                        "load_tool_result",
                        "keep_tool_result",
                    }
                    if self.policy.decide(name) == "allow"
                },
                max_retries=harness_cfg.code_mode_max_retries,
                max_tool_calls=harness_cfg.code_mode_max_tool_calls,
                max_duration_secs=harness_cfg.code_mode_max_duration_secs,
                max_memory=harness_cfg.code_mode_max_memory_bytes,
                max_suspensions=harness_cfg.code_mode_max_suspensions,
            )
            if harness_cfg.code_mode_enabled
            else None
        )
        prompt_parts = [load_system_prompt(), profile]
        if harness_cfg.code_mode_enabled:
            prompt_parts.append(CODE_GUIDANCE)
        prompt_parts.append(SKILL_GUIDANCE)
        if harness_cfg.instruction_addendum:
            prompt_parts.append(harness_cfg.instruction_addendum)
        self.system_prompt = "\n".join(prompt_parts)
        # Set by the composition root (bud serve, evals); auto mode then lets it review each
        # write. None means nothing is ever reviewed and auto mode asks like strict.
        self.reviewer: ToolReviewer | None = None
        # What each write's ids point to, rendered from the store: the reviewer's only view
        # of a target that is not spelled out in the call's own arguments.
        self.describers: dict[str, Describer] = {}
        if self.preferences is not None:
            self.describers["set_preference"] = self.preferences.describe_change
            self.describers["forget_preference"] = self.preferences.describe_change
        # Per tool: does this call notify people other than the owner? Computed in code.
        self.notifies_others: dict[str, Callable[[dict], bool]] = {}
        # Per tool: is this call harmless, judged by its arguments? It then runs unasked.
        self.harmless: dict[str, Callable[[dict], bool]] = {}
        # Per tool: a reason to refuse the call outright, judged in code, or None.
        self.refuses: dict[str, Callable[[dict], str | None]] = {}
        # Facts stamped on every approval card; see ActionGateway.stamp.
        self.stamp: Callable[[], dict[str, Any]] = pinned_account
        # Addresses the owner vouched for; see ActionGateway.known.
        self.known: KnownAddresses | None = None
        # Calls the owner approved after a hold, per session; see SessionGrants.
        self.grants = SessionGrants()
        # Instructions that must reflect live state (e.g. the approval mode) are
        # evaluated at the start of every run instead of being baked in at boot.
        self.live_instructions: list[Callable[..., str]] = [self._today]
        # The person's clock: real time, or the simulated world's fixed "now".
        self.clock: Callable[[], datetime] = lambda: datetime.now().astimezone()
        if self.preferences is not None:
            self.preferences.today = lambda: self.clock().date()
        memory_guidance = (memory.guidance or "") if memory is not None else ""
        prompt = self.system_prompt + memory_guidance
        self.prompt_version = sha256(prompt.encode()).hexdigest()[:12]
        self.spiral = SpiralConfig.from_profile(operating_profile.limits)
        settings = get_settings()
        # One daily cap for the whole install, so it lives in the data directory, not in
        # this account's session folder.
        self.spend = build_spend_limits(
            settings.data_dir / "spend.db", registry, settings.daily_spend_usd
        )

    def swap_tools(self, toolset: AbstractToolset[None]) -> None:
        """Replace the tools the next run sees; the running step keeps the ones it started with."""
        toolsets: list[AbstractToolset[None]] = [
            toolset,
            FunctionToolset[None](self.skills.tools()),
        ]
        if self.preferences is not None:
            toolsets.append(FunctionToolset[None](self.preferences.tools()))
        self.live.swap(CombinedToolset(toolsets))

    def _today(self) -> str:
        # Day granularity: instructions lead the prompt, so a per-minute clock here
        # would invalidate the cached conversation on every turn.
        now = self.clock()
        return (
            f"\nToday is {now:%A, %d %B %Y} (UTC{now:%z}). "
            "Call current_time when the exact time matters."
        )

    def build_context(self, record: Callable[[dict], None]) -> BackgroundCompaction:
        """Build the context compaction capability from the active operating profile."""
        ctx = self.operating_profile.context
        return BackgroundCompaction(
            record=record,
            result_store=self.results,
            max_fraction=ctx.max_fraction,
            background_fraction=ctx.background_fraction,
            fallback_context_window=ctx.fallback_context_window,
            keep_tokens=ctx.keep_tokens,
            compacted_target=ctx.compacted_target,
            clear_keep_pairs=ctx.clear_keep_pairs,
            clear_min_tokens=ctx.clear_min_tokens,
            summary_max_tokens=ctx.summary_max_tokens,
            summary_tool_return_max_chars=ctx.summary_tool_return_max_chars,
            history_spill_chars=self.operating_profile.tool_results.history_spill_chars,
            max_age_seconds=ctx.max_age_seconds,
            max_preparations=ctx.max_preparations,
        )

    def usage_limits(self) -> UsageLimits:
        """Build run usage limits from the active operating profile."""
        limits = self.operating_profile.limits
        return UsageLimits(
            request_limit=limits.request_limit,
            tool_calls_limit=limits.tool_calls_limit,
            total_tokens_limit=limits.total_tokens_limit,
        )

    def model_settings(self) -> ModelSettings | None:
        """Return profile-level request settings when any deviate from gateway defaults."""
        cfg = self.operating_profile.model
        settings: ModelSettings = {}
        if cfg.request_timeout_seconds != 120.0:
            settings["timeout"] = cfg.request_timeout_seconds
        if cfg.temperature is not None:
            settings["temperature"] = cfg.temperature
        if cfg.max_tokens is not None:
            settings["max_tokens"] = cfg.max_tokens
        if cfg.reasoning_effort is not None:
            settings["thinking"] = False if cfg.reasoning_effort == "none" else cfg.reasoning_effort
        return settings or None

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
        harness_cfg = self.operating_profile.harness
        limits = self.operating_profile.limits
        retries: int | AgentRetries = (
            {"tools": limits.retries, "output": limits.output_retries}
            if limits.output_retries is not None
            else limits.retries
        )
        reminders = (
            [
                SystemReminders(
                    reminders=[
                        Reminder(
                            LONG_RUN_REMINDER,
                            interval=harness_cfg.long_run_reminder_every,
                            tag=None,
                        )
                    ]
                )
            ]
            if harness_cfg.long_run_reminder_every is not None
            else []
        )
        return Agent(
            model,
            name="bud",
            deps_type=type(None),
            instructions=[self.system_prompt, *self.live_instructions],
            toolsets=[self.live],
            output_type=[str, DeferredToolRequests],
            model_settings=self.model_settings(),
            retries=retries,
            tool_timeout=limits.tool_timeout,
            end_strategy=limits.end_strategy,
            capabilities=[
                *([RepairToolArguments()] if harness_cfg.repair_tool_arguments else []),
                GuardTools(
                    policy=self.policy,
                    record=record,
                    reviewer=self.reviewer,
                    describers=self.describers,
                    notifies_others=self.notifies_others,
                    harmless=self.harmless,
                    refuses=self.refuses,
                    stamp=lambda: self.stamp(),
                    known=self.known,
                    facts=self._today,
                    grants=self.grants,
                ),
                self.output_limits,
                *([self.code_mode] if self.code_mode is not None else []),
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
                *([Caching()] if harness_cfg.caching_enabled else []),
                *([WarnOnCacheBusts()] if harness_cfg.warn_on_cache_busts else []),
                RaiseContentFilterError(),
                # Request-only reminders leave the cached history unchanged.
                *reminders,
                *([durability] if durability is not None else []),
                *self.capabilities,
                *capabilities,
            ],
        )
