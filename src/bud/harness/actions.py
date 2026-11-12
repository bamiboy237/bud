"""Apply tool policy and record decisions before dispatch.

Tools may also enforce their own safety checks.
"""

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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

from bud.harness.decisions import DecisionClient, evidence
from bud.harness.writes import decidable_tools, is_protected, self_hosted_writes

Action = Literal["allow", "ask", "deny", "decide"]
RecordDecision = Callable[[dict[str, Any]], None]


class Policy(BaseModel):
    """What each tool may do. "decide" means a decision model scores the call: a high score
    runs it, anything else asks the owner. Only balanced mode sets it (bud.core.profiles)."""

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
    decisions: DecisionClient | None = field(kw_only=True, default=None)
    # Trusted, non-owner context for the decision model, such as today's date.
    facts: Callable[[], str] = field(kw_only=True, default=lambda: "")

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        tools = await super().get_tools(ctx)
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    description=(
                        (
                            "Submit this change for review. A reviewer lets it run now or "
                            "the system asks the user first. "
                            if self.policy.decide(name) == "decide"
                            else "Submit this change for approval. "
                            "The system asks the user before execution. "
                        )
                        + (tool.tool_def.description or "")
                    ),
                ),
            )
            # ask_user asks for information, not permission; it needs no prefix.
            if self.policy.decide(name) in ("ask", "decide") and name != "ask_user"
            else tool
            for name, tool in tools.items()
            # A denied tool is never offered: the model cannot waste a turn on it.
            if self.policy.decide(name) != "deny"
        }

    async def _score(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> float:
        """The decision model's P(safe), or a refusal to score, which asks the owner."""
        # The allowlist is the real guard; the protected check is a second, explicit one.
        if is_protected(name) or name not in decidable_tools():
            raise ApprovalRequired()
        request = (ctx.metadata or {}).get("bud_request")
        if self.decisions is None or not isinstance(request, str) or not request.strip():
            raise ApprovalRequired()
        arguments = (
            name,
            tool_args,
            request,
            tool.tool_def.description or "",
            evidence(ctx.messages),
            self.facts(),
        )
        if DBOS.workflow_id is not None and DBOS.step_id is None:
            # A checkpointed step: crash recovery replays this score instead of asking the
            # model again, so a retry cannot flip a verdict the journal already recorded.
            return await DBOS.run_step_async(
                {"name": "bud.decision", "retries_allowed": False},
                self.decisions.safe_probability,
                *arguments,
            )
        return await self.decisions.safe_probability(*arguments)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        action = self.policy.decide(name)
        rule: str = action
        outcome = "refused" if action == "deny" else "allowed"
        score: float | None = None
        if action in ("ask", "decide") and ctx.tool_call_approved:
            rule, outcome = "ask", "approved"  # the owner already approved this exact call
        elif action == "ask":
            raise ApprovalRequired()
        elif action == "decide":
            score = await self._score(name, tool_args, ctx, tool)
            if self.decisions is None or score < self.decisions.threshold:
                raise ApprovalRequired()
            rule = "auto-allow"
        if action != "deny" and not ctx.tool_call_approved and name in self_hosted_writes():
            # The world and workspace writes refuse to run without an approval id, which they
            # use as a once-only key. A write the policy lets through gets a stable one, so a
            # replayed step cannot apply it twice.
            ctx = replace(
                ctx,
                tool_call_approved=True,
                tool_call_metadata={"approval_id": f"auto:{ctx.run_id}:{ctx.tool_call_id}"},
            )

        # The record comes first, so no tool can act without leaving a trace.
        record: dict[str, Any] = {
            "call_id": ctx.tool_call_id,
            "tool": name,
            "rule": rule,
            "outcome": outcome,
        }
        if score is not None:
            record["score"] = score
        self.record(record)
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
    decisions: DecisionClient | None = None
    facts: Callable[[], str] = lambda: ""
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
        return ActionGateway(
            toolset,
            policy=self.policy,
            record=self.record,
            decisions=self.decisions,
            facts=self.facts,
        )
