"""Read-only data processing through the native Monty Code Mode capability."""

from contextvars import ContextVar
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import CodeMode

inside_code_mode: ContextVar[bool] = ContextVar("bud_inside_code_mode", default=False)
CODE_TOOL_CALL_LIMIT = 12

CODE_GUIDANCE = """
Use `run_code` to process, calculate, and transform read-only tool outputs in an isolated sandbox.
Load stored tool payloads with `load_tool_result(handle=...)` and return targeted results.
State-modifying actions (such as sending messages, scheduling, or file edits) use their
dedicated external tools.
"""


class ReadOnlyCodeMode(CodeMode):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        if call.tool_name != "run_code":
            return await handler(args)
        token = inside_code_mode.set(True)
        try:
            return await handler(args)
        finally:
            inside_code_mode.reset(token)


def build_code_mode(read_tools: set[str]) -> ReadOnlyCodeMode:
    return ReadOnlyCodeMode(
        id="bud_read_only_code",
        tools=sorted(read_tools),
        max_retries=2,
        max_tool_calls=CODE_TOOL_CALL_LIMIT,
        resource_limits={
            "max_duration_secs": 10,
            "max_memory": 128 * 1024 * 1024,
            "max_suspensions": 2_000,
        },
    )
