"""A toolset whose contents can change between runs without rebuilding the agent.

The agent keeps one ``LiveTools`` object for its whole life. Swapping the inner toolset (a
new connection, a discovered app) applies to the next model request, never the one in
flight, because pydantic-ai reads the tools at the start of each run step. Copies made by
pydantic-ai share the same ``LiveState``, so a swap reaches them too.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool


@dataclass
class LiveState:
    current: AbstractToolset[None]
    pending: AbstractToolset[None] | None = None


@dataclass
class LiveTools(AbstractToolset[None]):
    state: LiveState
    active: ContextVar[AbstractToolset[None] | None] = field(
        default_factory=lambda: ContextVar("bud_live_tools", default=None)
    )

    @property
    def id(self) -> str | None:
        return "bud-live"

    @property
    def label(self) -> str:
        return f"LiveTools({self.state.current.label})"

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        return await (self.active.get() or self.state.current).get_tools(ctx)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        return await (self.active.get() or self.state.current).call_tool(name, tool_args, ctx, tool)

    async def __aenter__(self) -> LiveTools:
        # A swap requested during a run takes effect here, at the next run's start, so the
        # toolset entered is always the one exited.
        if self.state.pending is not None:
            self.state.current = self.state.pending
            self.state.pending = None
        active = self.state.current
        await active.__aenter__()
        self.active.set(active)
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        active = self.active.get()
        assert active is not None
        try:
            return await active.__aexit__(*args)
        finally:
            self.active.set(None)

    def apply(self, visitor) -> None:
        self.state.current.apply(visitor)

    def visit_and_replace(self, visitor) -> AbstractToolset[None]:
        # Update in place so every copy shares the same state.
        self.state.current = self.state.current.visit_and_replace(visitor)
        return self

    def swap(self, toolset: AbstractToolset[None]) -> None:
        """Stage a replacement; it takes effect at the start of the next run."""
        self.state.pending = toolset
