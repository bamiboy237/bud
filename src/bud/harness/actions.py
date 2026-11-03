"""The action gateway: the one checkpoint every tool call passes through.
`Policy` decides allow, ask, or deny for each tool, and `ActionGateway` records that decision
before the tool runs. Tools themselves carry no safety rules.
"""

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset

Action = Literal["allow", "ask", "deny"]
RecordDecision = Callable[[dict[str, Any]], None]


class Policy(BaseModel):
    tools: dict[str, Action]

    def decide(self, tool: str) -> Action:
        """Fails closed: a tool nobody classified is denied."""
        return self.tools.get(tool, "deny")


def load_policy(path: Path | None = None) -> Policy:
    if path is None:
        text = resources.files("bud.harness").joinpath("policy.toml").read_text()
    else:
        text = path.read_text()
    return Policy.model_validate(tomllib.loads(text))


@dataclass
class ActionGateway(WrapperToolset[None]):
    policy: Policy = field(kw_only=True)
    record: RecordDecision = field(kw_only=True)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        action = self.policy.decide(name)
        if action == "ask" and not ctx.tool_call_approved:
            raise ApprovalRequired()

        outcome = "refused" if action == "deny" else "approved" if action == "ask" else "allowed"
        # The record comes first, so no tool can act without leaving a trace.
        self.record({"call_id": ctx.tool_call_id, "tool": name, "rule": action, "outcome": outcome})
        if action == "deny":
            return f"Refused by policy: {name} is not allowed."
        return await super().call_tool(name, tool_args, ctx, tool)


def guard(toolset: AbstractToolset[None], policy: Policy, record: RecordDecision) -> ActionGateway:
    return ActionGateway(toolset, policy=policy, record=record)
