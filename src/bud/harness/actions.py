"""Apply tool policy and record decisions before dispatch.

Tools may also enforce their own safety checks.
"""

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from dbos import DBOS
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.exceptions import ApprovalRequired, UserError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
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
        if DBOS.workflow_id is not None and DBOS.step_id is None:
            return await DBOS.run_step_async(
                {"name": "bud.tool_call", "retries_allowed": False},
                super().call_tool,
                name,
                tool_args,
                ctx,
                tool,
            )
        return await super().call_tool(name, tool_args, ctx, tool)


@dataclass(kw_only=True)
class GuardTools(AbstractCapability[None]):
    policy: Policy
    record: RecordDecision
    id: str | None = "bud_action_gateway"

    async def before_tool_execute(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        # CodeMode creates run_code outside this wrapper; its nested reads still use it.
        if call.tool_name in {"run_code", "load_capability"}:
            if self.policy.decide(call.tool_name) != "allow":
                raise UserError("Code Mode is not allowed by the action policy")
            self.record(
                {
                    "call_id": ctx.tool_call_id,
                    "tool": call.tool_name,
                    "rule": "allow",
                    "outcome": "allowed",
                }
            )
        return args

    def get_wrapper_toolset(self, toolset: AbstractToolset[None]) -> ActionGateway:
        return ActionGateway(toolset, policy=self.policy, record=self.record)
