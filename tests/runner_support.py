import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.toolsets import FunctionToolset

from bud.core.history import conversation_search
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.runner import Runner
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from bud.harness.actions import Action, Policy
from bud.harness.agent import Harness
from bud.harness.ask import ask_user
from bud.harness.memory import build_memory
from bud.harness.preferences import PreferenceStore
from bud.harness.spiral import SpiralConfig
from bud.workspace import READ_TOOLS, WRITE_TOOLS, build_tools, read_capabilities

ResponseFunction = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted_model(
    function: ResponseFunction, *, profile: ModelProfile | None = None
) -> FunctionModel:
    async def stream(messages, info):
        response = function(messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=json.dumps(part.args_as_dict()),
                        tool_call_id=part.tool_call_id,
                    )
                }
            else:
                raise AssertionError(f"Unsupported scripted response: {part}")

    return FunctionModel(function, stream_function=stream, model_name="scripted", profile=profile)


@dataclass
class RunningBud:
    runner: Runner
    log: EventLog
    root: Path
    claims: Path

    async def settle(self):
        async with asyncio.timeout(10):
            while self.runner.tasks:
                await asyncio.gather(*list(self.runner.tasks.values()))
                await asyncio.sleep(0)


@asynccontextmanager
async def running_bud(
    directory: Path,
    function: ResponseFunction,
    *,
    jobs: bool = False,
    routines: bool | None = None,
    preferences: bool = False,
    ask: bool = False,
    spiral: SpiralConfig | None = None,
    extra_tools: list[Any] | None = None,
    extra_policy: dict[str, Action] | None = None,
    model_profile: ModelProfile | None = None,
):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "workspace"
    root.mkdir(exist_ok=True)
    log = EventLog(directory / "events.db")
    claims = directory / "dispatches.jsonl"
    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    registry = Registry(models=[spec])
    model = scripted_model(function, profile=model_profile)
    gateway = Gateway(registry, Ledger(directory / "costs.db"), lambda _: model)
    policy: dict[str, Action] = dict.fromkeys(READ_TOOLS, "allow")
    policy.update(dict.fromkeys(WRITE_TOOLS, "ask"))
    if extra_policy:
        policy.update(extra_policy)
    tools: list[Any] = [tool.function for tool in build_tools(root, claims)]
    if ask:
        policy["ask_user"] = "ask"
        tools.append(ask_user)
    if extra_tools:
        tools.extend(extra_tools)
    memory = build_memory(directory / "memory.db", namespace="owner") if preferences else None
    pref_store = (
        PreferenceStore(directory / "preferences.db", memory=memory) if preferences else None
    )
    harness = Harness(
        FunctionToolset[None](tools),
        Policy(tools=policy),
        registry,
        "A synthetic workspace for behavior tests.",
        memory=memory,
        preferences=pref_store,
        state_dir=directory,
        result_path=log.path,
        read_tools=READ_TOOLS,
        capabilities=[*read_capabilities(root), conversation_search(log)],
    )
    harness.clock = lambda: datetime(2026, 11, 6, 12, tzinfo=UTC)
    if spiral is not None:
        harness.spiral = spiral
    runner = Runner(Hub(log), gateway, harness, jobs=jobs, routines=routines, workspace=root)
    try:
        yield RunningBud(runner, log, root, claims)
    finally:
        await runner.stop()
        gateway.close()


def create_note(content: str = "Cedar first draft", *, call_id: str = "write-note"):
    return ModelResponse(
        parts=[
            ToolCallPart(
                "workspace_create",
                {"path": "note.txt", "content": content},
                tool_call_id=call_id,
            )
        ]
    )


def reply(text: str = "Finished"):
    return ModelResponse(parts=[TextPart(text)])
