"""Shared harness and runner composition for production server and evals."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic_ai import Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from bud.config import Settings
from bud.core.history import conversation_search
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.profiles import Mode, apply_mode
from bud.core.profiles import guidance as mode_guidance
from bud.core.runner import Runner
from bud.gateway import Gateway
from bud.harness.actions import Policy, load_policy
from bud.harness.agent import Harness
from bud.harness.learning import Learner
from bud.harness.memory import build_memory
from bud.harness.operating_profile import BASELINE_PROFILE, OperatingProfile
from bud.harness.preferences import PreferenceStore
from bud.harness.recall import Recall, embedder
from bud.harness.review import ToolReviewer
from bud.harness.taint import KnownAddresses
from bud.web import READ_TOOLS as WEB_READ_TOOLS
from bud.web import Cassette
from bud.web import build_tools as build_web_tools
from bud.world.describe import build_describers, build_notifies_others
from bud.world.store import World
from bud.world.tools import READ_TOOLS as WORLD_READ_TOOLS
from bud.world.tools import build_tools as build_world_tools


def compose_world_harness(
    world: World,
    gateway: Gateway,
    settings: Settings,
    *,
    state_dir: Path,
    result_path: Path,
    memory_path: Path,
    operating_profile: OperatingProfile = BASELINE_PROFILE,
    extra_tools: Sequence[Tool[None]] = (),
    extra_toolsets: Sequence[AbstractToolset[None]] = (),
    policy: Policy | None = None,
    extra_read_tools: set[str] | None = None,
    prompt_suffix: str = "",
    include_ask_user: bool = True,
    cassette: Cassette | None = None,
    result_retention: timedelta | None = None,
    capabilities: Sequence[AbstractCapability[None]] = (),
    known_addresses: KnownAddresses | None = None,
) -> Harness:
    """Build a simulated-world Harness shared by `bud serve --simulate` and `bud eval`."""
    tools_policy = policy or load_policy()
    tools_policy.tools.update({tool.name: "allow" for tool in extra_tools})
    tools_policy.tools.update(dict.fromkeys(WEB_READ_TOOLS, "allow"))
    toolsets: list[AbstractToolset[None]] = [
        FunctionToolset(
            [*build_world_tools(world, include_ask_user=include_ask_user), *extra_tools]
        ),
        build_web_tools(settings, cassette=cassette),
        *extra_toolsets,
    ]
    read_tools = set(WORLD_READ_TOOLS) | set(WEB_READ_TOOLS) | set(extra_read_tools or ())
    memory = build_memory(memory_path, namespace="world")
    preferences = PreferenceStore(memory_path, memory=memory)
    harness = Harness(
        CombinedToolset(toolsets),
        tools_policy,
        gateway.registry,
        world.profile() + prompt_suffix,
        memory=memory,
        preferences=preferences,
        state_dir=state_dir,
        result_path=result_path,
        read_tools=read_tools,
        result_retention=result_retention,
        capabilities=capabilities,
        operating_profile=operating_profile,
    )
    harness.describers = build_describers(world)
    harness.notifies_others = build_notifies_others(world)
    if known_addresses is not None:
        harness.known = known_addresses
    harness.clock = lambda: datetime.fromisoformat(world.now)
    return harness


def compose_runner(
    hub: Hub,
    gateway: Gateway,
    harness: Harness,
    log: EventLog,
    settings: Settings,
    *,
    approval_mode: Mode,
    recall_path: Path,
    reviewer: ToolReviewer | None = None,
    grants: frozenset[str] | None = None,
    durable: bool = False,
    jobs: bool = True,
    workspace: Path | None = None,
    include_history_search: bool = True,
    mode_holder: dict[str, Mode] | None = None,
    runner_cls: Callable[..., Runner] = Runner,
) -> Any:
    """Apply approval mode, memory/recall, and build the Runner + Agent via one path."""
    harness.reviewer = reviewer
    if grants is not None:
        harness.policy.grants = grants
    has_reviewer = harness.reviewer is not None
    apply_mode(harness.policy, approval_mode, reviewer=has_reviewer)
    live_mode = mode_holder if mode_holder is not None else {"mode": approval_mode}
    harness.live_instructions.append(
        lambda: mode_guidance(live_mode["mode"], reviewer=has_reviewer)
    )
    if include_history_search:
        harness.capabilities.append(
            conversation_search(log, deferred=harness.operating_profile.harness.defer_capabilities)
        )
    profile = harness.operating_profile
    learner = (
        Learner(
            harness.memory,
            lambda session_id: gateway.model(
                profile.model.tier,
                f"{session_id}:learn",
                require_tools=True,
                selected=profile.model.model_id,
            ),
            preferences=harness.preferences,
        )
        if harness.memory is not None
        else None
    )
    recall = Recall(
        recall_path,
        embedder=embedder(settings),
        memory=harness.memory,
        workspace=workspace,
    )
    return runner_cls(
        hub,
        gateway,
        harness,
        durable=durable,
        jobs=jobs,
        learner=learner,
        recall=recall,
        workspace=workspace,
        operating_profile=profile,
    )
