"""Apply tool policy and record decisions before dispatch.

Tools may also enforce their own safety checks.
"""

import tomllib
from collections.abc import Callable, Mapping
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

from bud.harness.review import (
    Describer,
    ReviewRequest,
    ReviewResult,
    SessionGrants,
    ToolReviewer,
    Verdict,
    held,
    owner_answers,
)
from bud.harness.writes import never_reviewed, reviewable_tools, self_hosted_writes

Action = Literal["allow", "ask", "deny", "review"]
RecordDecision = Callable[[dict[str, Any]], None]


class Policy(BaseModel):
    """What each tool may do. "review" means the approval reviewer judges the call: if it is
    safe and clearly requested the call runs, otherwise the owner is asked. Only auto mode
    sets it (bud.core.profiles)."""

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
    reviewer: ToolReviewer | None = field(kw_only=True, default=None)
    # Renders what a call's ids refer to from bud's own store, per tool name.
    describers: Mapping[str, Describer] = field(kw_only=True, default_factory=dict)
    # Hard rule: a write that would notify people other than the owner (an invitation, a
    # moved or cancelled meeting) always waits for the owner, in every mode.
    notifies_others: Mapping[str, Callable[[dict], bool]] = field(
        kw_only=True, default_factory=dict
    )
    # Trusted, non-owner context for the reviewer, such as today's date.
    facts: Callable[[], str] = field(kw_only=True, default=lambda: "")
    grants: SessionGrants = field(kw_only=True, default_factory=SessionGrants)

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
                            if self.policy.decide(name) == "review"
                            else "Submit this change for approval. "
                            "The system asks the user before execution. "
                        )
                        + (tool.tool_def.description or "")
                    ),
                ),
            )
            # ask_user asks for information, not permission; it needs no prefix.
            if self.policy.decide(name) in ("ask", "review") and name != "ask_user"
            else tool
            for name, tool in tools.items()
            # A denied tool is never offered: the model cannot waste a turn on it.
            if self.policy.decide(name) != "deny"
        }

    def _request(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> ReviewRequest | None:
        """The stripped reviewer input, or None when there is no owner request to judge by."""
        request = (ctx.metadata or {}).get("bud_request")
        if not isinstance(request, str) or not request.strip():
            return None
        details: str | None = None
        describe = self.describers.get(name)
        if describe is not None:
            try:
                details = describe(tool_args)
            except Exception:
                details = "The target could not be resolved from bud's records."
        return ReviewRequest(
            tool=name,
            args=tool_args,
            description=tool.tool_def.description or "",
            details=details,
            owner_request=request,
            clarifications=owner_answers(ctx.messages),
            today=self.facts(),
        )

    def _notifies_others(self, name: str, tool_args: dict[str, Any]) -> bool:
        check = self.notifies_others.get(name)
        if check is None:
            return False
        try:
            return check(tool_args)
        except Exception:
            return True  # unsure who would be notified: the owner decides

    async def _review(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> ReviewResult:
        """Judge one call. Anything but an explicit ALLOW means the owner is asked."""
        # The policy overlay already keeps these off "review"; this is a second, explicit guard.
        if never_reviewed(name) or name not in reviewable_tools():
            return held("This kind of change always needs the owner.")
        if self.reviewer is None:
            return held("No reviewer is configured, so the owner decides.")
        request = self._request(name, tool_args, ctx, tool)
        if request is None:
            return held("There is no owner request to judge this call against.")
        session = (ctx.metadata or {}).get("bud_session_id")
        if isinstance(session, str) and self.grants.has(session, request):
            return ReviewResult(
                verdict=Verdict.ALLOW,
                tier="session",
                reason="The owner approved this exact call earlier in this session.",
            )
        if DBOS.workflow_id is not None and DBOS.step_id is None:
            # A checkpointed step: crash recovery replays this verdict instead of asking the
            # model again, so a retry cannot flip a verdict the journal already recorded.
            return await DBOS.run_step_async(
                {"name": "bud.review", "retries_allowed": False}, self.reviewer.review, request
            )
        return await self.reviewer.review(request)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        action = self.policy.decide(name)
        if action == "review" and self._notifies_others(name, tool_args):
            action = "ask"  # hard rule: other people would be notified
        rule: str = action
        outcome = "refused" if action == "deny" else "allowed"
        review: dict[str, Any] | None = None
        if action in ("ask", "review") and ctx.tool_call_approved:
            rule, outcome = "ask", "approved"  # the owner already approved this exact call
            held_review = (ctx.tool_call_metadata or {}).get("review")
            if isinstance(held_review, dict):
                review = held_review
                session = (ctx.metadata or {}).get("bud_session_id")
                request = self._request(name, tool_args, ctx, tool)
                if action == "review" and isinstance(session, str) and request is not None:
                    self.grants.remember(session, request)
        elif action == "ask":
            raise ApprovalRequired()
        elif action == "review":
            result = await self._review(name, tool_args, ctx, tool)
            review = result.public()
            if not result.allowed:
                self.record(
                    {
                        "call_id": ctx.tool_call_id,
                        "tool": name,
                        "rule": "review",
                        "outcome": "asked",
                        "review": review,
                    }
                )
                # The runner stores this beside the approval request and hands it back when
                # the owner decides, so a denial can carry the reviewer's concern.
                raise ApprovalRequired(metadata=review)
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
        if review is not None:
            record["review"] = review
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
    reviewer: ToolReviewer | None = None
    describers: Mapping[str, Describer] = field(default_factory=dict)
    notifies_others: Mapping[str, Callable[[dict], bool]] = field(default_factory=dict)
    facts: Callable[[], str] = lambda: ""
    # One set of grants for the life of the process, shared by every wrapper built from this.
    grants: SessionGrants = field(default_factory=SessionGrants)
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
            reviewer=self.reviewer,
            describers=self.describers,
            notifies_others=self.notifies_others,
            facts=self.facts,
            grants=self.grants,
        )
