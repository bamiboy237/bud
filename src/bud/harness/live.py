"""A toolset whose contents can change between runs without rebuilding the agent.

The agent keeps one ``LiveTools`` object for its whole life. Swapping the inner toolset (a
new connection, a discovered app) applies to the next model request, never the one in
flight, because pydantic-ai reads the tools at the start of each run step. Copies made by
pydantic-ai share the same ``LiveState``, so a swap reaches them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool


@dataclass
class LiveState:
    current: AbstractToolset[None]


@dataclass
class LiveTools(AbstractToolset[None]):
    state: LiveState

    @property
    def id(self) -> str | None:
        return "bud-live"

    @property
    def label(self) -> str:
        return f"LiveTools({self.state.current.label})"

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        return await self.state.current.get_tools(ctx)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        return await self.state.current.call_tool(name, tool_args, ctx, tool)

    async def __aenter__(self) -> LiveTools:
        await self.state.current.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        return await self.state.current.__aexit__(*args)

    def apply(self, visitor) -> None:
        self.state.current.apply(visitor)

    def visit_and_replace(self, visitor) -> AbstractToolset[None]:
        # Update in place so every copy shares the same state.
        self.state.current = self.state.current.visit_and_replace(visitor)
        return self

    def swap(self, toolset: AbstractToolset[None]) -> None:
        """Replace the tools the next run sees. Call this between runs, not during one."""
        self.state.current = toolset
