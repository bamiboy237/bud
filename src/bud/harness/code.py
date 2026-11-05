"""Read-only data processing through the native Monty Code Mode capability."""

from contextvars import ContextVar
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import CodeMode

inside_code_mode: ContextVar[bool] = ContextVar("bud_inside_code_mode", default=False)

CODE_GUIDANCE = """
Use run_code to filter, join, group, and calculate over read-only tool results.
Fetch data through the listed sandbox functions and return only the answer and source identifiers.
Do not print or return an entire dataset when a small result answers the question.
load_tool_result(handle=...) loads a saved result inside the sandbox, decoding JSON when possible.
Sends, drafts, calendar changes, and memory changes are not sandbox functions.
Use their normal tools.
The sandbox has no host files, environment, network, or arbitrary third-party Python packages.
Variables can be reused within this run, but do not assume they exist in another run.
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
        max_tool_calls=12,
        resource_limits={
            "max_duration_secs": 10,
            "max_memory": 128 * 1024 * 1024,
            "max_suspensions": 2_000,
        },
    )
