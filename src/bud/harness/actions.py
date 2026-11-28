"""Apply tool policy and record decisions before dispatch.

Tools may also enforce their own safety checks.
"""

import asyncio
import contextlib
import inspect
import tomllib
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from dbos import DBOS, StepOptions
from pydantic import AfterValidator, BaseModel, ConfigDict
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.exceptions import ApprovalRequired, UserError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset

from bud.harness.email_preview import email_preview, unavailable_email_preview
from bud.harness.review import (
    Describer,
    ReviewRequest,
    ReviewResult,
    SessionGrants,
    ToolReviewer,
    Verdict,
    held,
    owner_answers,
    too_long_to_review,
)
from bud.harness.taint import (
    ACCOUNT_TOOL,
    KnownAddresses,
    owner_replies,
    owner_text,
    read_text,
    recipients,
    sent_query,
    sent_to,
    tainted,
)
from bud.harness.taint import note as taint_note
from bud.harness.writes import (
    consequential_tools,
    never_reviewed,
    reviewable_tools,
    self_hosted_writes,
)

Action = Literal["allow", "ask", "deny", "review"]
RecordDecision = Callable[[dict[str, Any]], None]
PriorDispatchCheck = Callable[[str, str, str], str | None]
COMPOSIO_ACTION = "bud_composio_action"
BROWSER_TARGET = "browser_target"
STALE_BROWSER_TARGET = {
    "successful": False,
    "error": "The browser page changed or could not be verified. Nothing was done. "
    "Check the current page and request fresh approval.",
}
SEND_ACTIONS = (
    "send_email",
    "send_mail",
    "send_message",
    "send_draft",
    "send_sms",
    "reply_email",
    "reply_message",
    "reply_to_email",
    "reply_to_thread",
    "forward_email",
    "post_message",
)


def require_full_preview(tool: str) -> bool:
    name = tool.lower()
    return name == "gmail_send" or any(
        name == action or name.endswith(f"_{action}") for action in SEND_ACTIONS
    )


class TargetDescription(str):
    """Display text with a fingerprint of the browser page that supplied it."""

    fingerprint: str | None

    def __new__(cls, text: str, fingerprint: str | None) -> Self:
        description = super().__new__(cls, text)
        description.fingerprint = fingerprint
        return description


class _ToolRules(dict[str, Action]):
    """Mode overlays use update. An explicit policy edit can replace a deny directly."""

    def update(self, *args: Any, **kwargs: Action) -> None:
        incoming: dict[str, Action] = dict(*args, **kwargs)
        super().update(
            (name, action) for name, action in incoming.items() if self.get(name) != "deny"
        )

    def copy(self) -> Self:
        return type(self)(self)


class UncertainSideEffectError(Exception):
    """A consequential tool already recorded permission before a crash with no confirmed result."""

    def __init__(self, tool: str, call_id: str) -> None:
        self.tool = tool
        self.call_id = call_id
        super().__init__(f"Uncertain outcome for {tool} ({call_id}): inspect before retrying.")


class Policy(BaseModel):
    """What each tool may do. "review" means the approval reviewer judges the call: if it is
    safe and clearly requested the call runs, otherwise the owner is asked. Only auto mode
    sets it (bud.core.profiles). A standing grant from the owner turns that tool's "ask" or
    "review" into "allow"; hard rules can never be granted."""

    model_config = ConfigDict(validate_assignment=True)

    tools: Annotated[dict[str, Action], AfterValidator(_ToolRules)]
    grants: frozenset[str] = frozenset()
    no_grants: frozenset[str] = frozenset()
    always_ask: frozenset[str] = frozenset()

    def decide(self, tool: str) -> Action:
        """Fails closed: a tool nobody classified is denied."""
        action = self.tools.get(tool)
        if action is None or action == "deny":
            return "deny"
        if tool in self.always_ask:
            return "ask"
        if tool in self.grants and tool not in self.no_grants and not never_reviewed(tool):
            return "allow"
        return action

    def decide_tool(self, tool: ToolDefinition) -> Action:
        """Like decide, for app actions found at run time.

        A found action that only reads runs without asking. Any other found action asks
        every time and never inherits a standing grant.
        """
        found = (tool.metadata or {}).get(COMPOSIO_ACTION)
        if tool.name not in self.tools and found is not None:
            if tool.name in self.always_ask:
                return "ask"
            return "allow" if isinstance(found, dict) and found.get("read_only") is True else "ask"
        return self.decide(tool.name)

    def grantable(self, tool: str) -> bool:
        """True when the owner may make this tool run without asking again."""
        return (
            self.tools.get(tool) in ("ask", "review")
            and tool not in self.no_grants
            and tool not in self.always_ask
            and not never_reviewed(tool)
        )


def load_policy(path: Path | None = None) -> Policy:
    if path is None:
        text = resources.files("bud.harness").joinpath("policy.toml").read_text()
    else:
        text = path.read_text()
    return Policy.model_validate(tomllib.loads(text))


# The account generation of the account tools this run entered (set by bud.composio.tools).
# Every approval card is stamped with it, so the card names the account it was asked under,
# even if the binding changes while the card is drawn.
ACCOUNT_GENERATION: ContextVar[int | None] = ContextVar("bud_account_generation", default=None)


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("successful") is False


def _pinned() -> dict[str, Any]:
    generation = ACCOUNT_GENERATION.get()
    return {"account_generation": generation} if generation is not None else {}


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
    # Per tool: is this call harmless, judged by its arguments in code? Such a call of a tool
    # that can also do more (browser_tabs lists, opens, and closes) runs unasked.
    harmless: Mapping[str, Callable[[dict], bool]] = field(kw_only=True, default_factory=dict)
    # Per tool: a reason to refuse this call outright, judged in code, or None. A refusal
    # never runs the tool, so it is safe even if the arguments' target changes later.
    refuses: Mapping[str, Callable[[dict], str | None]] = field(kw_only=True, default_factory=dict)
    # Facts stamped on every approval card, such as the account generation it was asked
    # under, so the tool can refuse an approval given for an account that has since changed.
    stamp: Callable[[], dict[str, Any]] = field(kw_only=True, default=_pinned)
    # Addresses the owner vouched for; a tainted one that is known needs no card.
    known: KnownAddresses | None = field(kw_only=True, default=None)
    # Trusted, non-owner context for the reviewer, such as today's date.
    facts: Callable[[], str] = field(kw_only=True, default=lambda: "")
    grants: SessionGrants = field(kw_only=True, default_factory=SessionGrants)
    prior_dispatch: PriorDispatchCheck | None = field(kw_only=True, default=None)

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
                            if self.policy.decide_tool(tool.tool_def) == "review"
                            else "Submit this change for approval. "
                            "The system asks the user before execution. "
                        )
                        + (tool.tool_def.description or "")
                    ),
                ),
            )
            # ask_user asks for information, not permission; it needs no prefix.
            if self.policy.decide_tool(tool.tool_def) in ("ask", "review") and name != "ask_user"
            else tool
            for name, tool in tools.items()
            # A denied tool is never offered: the model cannot waste a turn on it.
            if self.policy.decide_tool(tool.tool_def) != "deny"
        }

    async def _describe(self, name: str, tool_args: dict[str, Any]) -> str | None:
        """What a call's ids refer to, looked up once per call so the reviewer and the owner's
        card see the same facts. A describer may be async, such as one that asks Browse."""
        describe = self.describers.get(name)
        if describe is None:
            return None
        try:
            details = describe(tool_args)
            return await details if inspect.isawaitable(details) else details
        except Exception:
            return "The target could not be resolved from bud's records."

    def _request(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
        details: str | None,
    ) -> ReviewRequest | None:
        """The stripped reviewer input, or None when there is no owner request to judge by."""
        request = (ctx.metadata or {}).get("bud_request")
        if not isinstance(request, str) or not request.strip():
            return None
        return ReviewRequest(
            tool=name,
            args=tool_args,
            description=tool.tool_def.description or "",
            details=details,
            owner_request=request,
            clarifications=owner_answers(ctx.messages),
            today=self.facts(),
        )

    def _checks_targets(self, tool: ToolDefinition) -> bool:
        """Whether a call's targets matter: every write that reaches outward, judged by the
        tool's own rule before any standing grant. Reads, Gmail and Calendar ones included, are
        not checked; an app action found at run time is checked unless it only reads."""
        name = tool.name
        if name in consequential_tools() or name in {"browser_upload", "read_webpage"}:
            return True
        found = (tool.metadata or {}).get(COMPOSIO_ACTION)
        if name not in self.policy.tools and found is not None:
            return not (isinstance(found, dict) and found.get("read_only") is True)
        return bool(ACCOUNT_TOOL.fullmatch(name)) and self.policy.tools.get(name) != "allow"

    def _tainted(
        self, tool: ToolDefinition, tool_args: dict[str, Any], ctx: RunContext[None]
    ) -> list[str]:
        if not self._checks_targets(tool):
            return []
        request = (ctx.metadata or {}).get("bud_request")
        # The owner's own words: this request, their earlier messages, and their answers.
        # Never Bud's questions: the model wrote those, and an injection could plant a target
        # in one.
        # In a job or routine occurrence, the messages hold a system-written brief/prompt:
        # only the owner's own request (bud_request) and replies count as owner words.
        in_job = (ctx.metadata or {}).get("bud_task_id") is not None or (ctx.metadata or {}).get(
            "bud_occurrence_id"
        ) is not None
        said = [
            request if isinstance(request, str) else "",
            "" if in_job else owner_text(ctx.messages),
            *owner_replies(ctx.messages),
        ]
        read = read_text(ctx.messages, ctx.tool_call_id)
        if in_job:
            # The brief may carry a target from content the main chat read: it is read content.
            read = f"{read}\n{owner_text(ctx.messages)}"
        try:
            # Only content from outside can show a link as given: not the model's own words or
            # calls, and not a job's brief, which the model wrote.
            given = read_text(ctx.messages, ctx.tool_call_id, model=False)
            return tainted(tool.name, tool_args, "\n".join(said), read, given)
        except Exception:
            return []  # unsure: the call keeps its policy

    async def _unknown(
        self, name: str, tool_args: dict[str, Any], found: list[str], ctx: RunContext[None]
    ) -> list[str]:
        """Drop tainted recipients the owner already knows. Only a sending call's recipients:
        an address someone emailed does not vouch for a command or a link naming its domain."""
        sends_to = recipients(name, tool_args)
        return [
            target
            for target in found
            if target not in sends_to or not await self._known(target, ctx)
        ]

    async def _known(self, address: str, ctx: RunContext[None]) -> bool:
        """Whether the owner vouched for `address`, for the account generation this run
        entered: approved it as a recipient before, or emailed it from Gmail. Any failure
        counts as unknown, so the card still shows."""
        known = self.known
        if known is None:
            return False
        generation = ACCOUNT_GENERATION.get()
        search = "GMAIL_FETCH_EMAILS"

        async def check() -> bool:
            if known.has(address, generation):
                return True
            if self.policy.decide(search) != "allow":
                return False
            tools = await super(ActionGateway, self).get_tools(ctx)
            if search not in tools:
                return False
            async with asyncio.timeout(8):
                result = await super(ActionGateway, self).call_tool(
                    search,
                    {"query": sent_query(address), "max_results": 5, "user_id": "me"},
                    replace(ctx, tool_name=search),
                    tools[search],
                )
            sent = sent_to(result, address)
            if sent:
                known.add([address], generation)
            return sent

        try:
            if DBOS.workflow_id is not None and DBOS.step_id is None:
                # One step for the whole answer, the remembered list included, so recovery
                # replays it in the same place instead of skipping or repeating a search.
                return await DBOS.run_step_async(
                    {"name": "bud.known_address", "retries_allowed": False}, check
                )
            return await check()
        except Exception:
            return False

    def _refusal(self, name: str, tool_args: dict[str, Any]) -> str | None:
        check = self.refuses.get(name)
        if check is None:
            return None
        try:
            reason = check(tool_args)
        except Exception:
            return None  # unsure: the call keeps its policy, which still asks
        return reason if isinstance(reason, str) and reason else None

    def _harmless(self, name: str, tool_args: dict[str, Any]) -> bool:
        check = self.harmless.get(name)
        if check is None:
            return False
        try:
            return check(tool_args) is True
        except Exception:
            return False  # unsure: the call keeps its policy

    def _notifies_others(self, name: str, tool_args: dict[str, Any]) -> bool:
        check = self.notifies_others.get(name)
        if check is None:
            return False
        try:
            return check(tool_args)
        except Exception:
            return True  # unsure who would be notified: the owner decides

    async def _review(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
        details: str | None,
        target: str | None,
    ) -> ReviewResult:
        """Judge one call. Anything but an explicit ALLOW means the owner is asked."""
        # The policy overlay already keeps these off "review"; this is a second, explicit guard.
        if name in self.policy.always_ask or never_reviewed(name) or name not in reviewable_tools():
            return held("This kind of change always needs the owner.")
        if self.reviewer is None:
            return held("No reviewer is configured, so the owner decides.")
        request = self._request(name, tool_args, ctx, tool, details)
        if request is None:
            return held("There is no owner request to judge this call against.")
        session = (ctx.metadata or {}).get("bud_session_id")
        if (
            target is None
            and name not in self.policy.no_grants
            and isinstance(session, str)
            and self.grants.has(session, request)
        ):
            return ReviewResult(
                verdict=Verdict.ALLOW,
                tier="session",
                reason="The owner approved this exact call earlier in this session.",
            )
        if DBOS.workflow_id is not None and DBOS.step_id is None:
            # A checkpointed step: crash recovery replays this verdict instead of asking the
            # model again, so a retry cannot flip a verdict the journal already recorded.
            options: StepOptions = {"name": "bud.review", "retries_allowed": False}
            review = self.reviewer.review
            if target is None:
                return await DBOS.run_step_async(options, review, request)

            async def review_page() -> tuple[ReviewResult, str]:
                return await review(request), target

            replay = await DBOS.run_step_async(options, review_page)
            if not isinstance(replay, tuple) or replay[1] != target:
                return held("The browser page changed since review. Request fresh approval.")
            return replay[0]
        return await self.reviewer.review(request)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        action = self.policy.decide_tool(tool.tool_def)
        refusal = self._refusal(name, tool_args)
        if refusal is not None:
            action = "deny"
        if (
            action in ("ask", "review")
            and name not in self.policy.always_ask
            and self._harmless(name, tool_args)
        ):
            action = "allow"
        found = (
            self._tainted(tool.tool_def, tool_args, ctx)
            if action != "deny" and not ctx.tool_call_approved
            else []
        )
        found = await self._unknown(name, tool_args, found, ctx) if found else found
        if found and action in ("allow", "review"):
            action = "ask"  # a target came from what Bud read, not from the owner
        if action == "review" and self._notifies_others(name, tool_args):
            action = "ask"  # hard rule: other people would be notified
        if action == "review" and too_long_to_review(tool_args):
            action = "ask"  # the reviewer would see only part of the call
        rule: str = action
        outcome = "refused" if action == "deny" else "allowed"
        details = (
            await self._describe(name, tool_args)
            if action in ("ask", "review") and not ctx.tool_call_approved
            else None
        )
        target = details.fingerprint if isinstance(details, TargetDescription) else None
        if isinstance(details, TargetDescription) and target is None:
            return STALE_BROWSER_TARGET.copy()
        if found:
            details = "\n".join(part for part in (details, taint_note(found)) if part)
        review: dict[str, Any] | None = None
        # Recipients the owner approved, remembered once the call has gone through.
        learned: set[str] = set()
        if ctx.tool_call_approved and action != "deny" and self._checks_targets(tool.tool_def):
            # A call whose rule is "allow" was held only by the target check, so running it
            # now is an owner approval too.
            if action == "allow":
                rule, outcome = "ask", "approved"
            if "draft" not in name.lower():
                learned = recipients(name, tool_args)
        if action in ("ask", "review") and ctx.tool_call_approved:
            rule, outcome = "ask", "approved"  # the owner already approved this exact call
            held_review = (ctx.tool_call_metadata or {}).get("review")
            if isinstance(held_review, dict):
                review = held_review
                session = (ctx.metadata or {}).get("bud_session_id")
                request = self._request(name, tool_args, ctx, tool, details)
                # A tool that offers no standing grant gets no session grant either: a
                # browser step approved on one page must not replay on another.
                if (
                    action == "review"
                    and BROWSER_TARGET not in held_review
                    and name not in self.policy.no_grants
                    and isinstance(session, str)
                    and request is not None
                ):
                    self.grants.remember(session, request)
        elif action == "ask":
            raise ApprovalRequired(
                metadata=await self._presentation(name, tool_args, ctx, details, target)
            )
        elif action == "review":
            result = await self._review(name, tool_args, ctx, tool, details, target)
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
                raise ApprovalRequired(
                    metadata={
                        **review,
                        **await self._presentation(name, tool_args, ctx, details, target),
                    }
                )
            rule = "auto-allow"
        if ctx.tool_call_approved:
            metadata = ctx.tool_call_metadata or {}
            held_review = metadata.get("review")
            approved_target = (
                held_review.get(BROWSER_TARGET)
                if isinstance(held_review, dict)
                else metadata.get(BROWSER_TARGET)
            )
            target = approved_target if isinstance(approved_target, str) else None
        if target is not None:
            ctx = replace(
                ctx, tool_call_metadata={**(ctx.tool_call_metadata or {}), BROWSER_TARGET: target}
            )
        if action != "deny" and not ctx.tool_call_approved and name in self_hosted_writes():
            # The world and workspace writes refuse to run without an approval id, which they
            # use as a once-only key. A write the policy lets through gets a stable one, so a
            # replayed step cannot apply it twice.
            ctx = replace(
                ctx,
                tool_call_approved=True,
                tool_call_metadata={"approval_id": f"auto:{ctx.run_id}:{ctx.tool_call_id}"},
            )

        # Check before recording this attempt: if a prior crash happened mid-tool after
        # recording permission, DBOS step replay enters `_dispatch` only when the step never
        # recorded an output, so we inspect and refuse rather than repeating the side effect.
        session_id = (ctx.metadata or {}).get("bud_session_id")
        prior_uncertain = (
            self.prior_dispatch(session_id, ctx.run_id, ctx.tool_call_id)
            if self.prior_dispatch is not None
            and isinstance(session_id, str)
            and ctx.run_id is not None
            and ctx.tool_call_id is not None
            and action != "deny"
            and name not in self_hosted_writes()
            and name not in {"install_skill", "disable_skill", "remind_me", "run_in_background"}
            and (self._checks_targets(tool.tool_def) or name in consequential_tools())
            else None
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
            return refusal or f"Refused by policy: {name} is not allowed."

        async def _dispatch() -> Any:
            if prior_uncertain is not None:
                raise UncertainSideEffectError(name, ctx.tool_call_id or "")
            return await super(ActionGateway, self).call_tool(name, tool_args, ctx, tool)

        if DBOS.workflow_id is not None and DBOS.step_id is None:
            result = await DBOS.run_step_async(
                {"name": "bud.tool_call", "retries_allowed": False},
                _dispatch,
            )
        else:
            result = await _dispatch()
        # Only after the tool ran: an approval the account tools refused (another account,
        # a stale card) raises above and teaches nothing.
        if learned and self.known is not None and not _failed(result):
            with contextlib.suppress(OSError):
                self.known.add(learned, ACCOUNT_GENERATION.get())
        return result

    async def _presentation(
        self, name: str, args: dict, ctx: RunContext[None], details: str | None, target: str | None
    ) -> dict:
        async def read(read_name: str, read_args: dict) -> Any:
            if self.policy.decide(read_name) != "allow":
                raise ValueError("Preview read is not allowed")
            tools = await super(ActionGateway, self).get_tools(ctx)
            return await super(ActionGateway, self).call_tool(
                read_name, read_args, replace(ctx, tool_name=read_name), tools[read_name]
            )

        try:
            async with asyncio.timeout(10):
                preview = await email_preview(name, args, read)
        except Exception:
            # Presentation failure must not suppress the exact approval or expose connector errors.
            preview = unavailable_email_preview(name, args)
        shown: dict[str, Any] = {"email_preview": preview.model_dump()} if preview else {}
        # No stamp on failure: the tool's own binding fence still applies.
        with contextlib.suppress(Exception):
            shown.update(self.stamp())
        if details is not None:
            # The card shows what the reviewer saw: facts Bud looked up, never model text.
            shown["details"] = str(details)
        if target is not None:
            shown[BROWSER_TARGET] = target
        if require_full_preview(name):
            shown["require_full_preview"] = True
        return shown


@dataclass(kw_only=True)
class GuardTools(AbstractCapability[None]):
    policy: Policy
    record: RecordDecision
    reviewer: ToolReviewer | None = None
    describers: Mapping[str, Describer] = field(default_factory=dict)
    notifies_others: Mapping[str, Callable[[dict], bool]] = field(default_factory=dict)
    harmless: Mapping[str, Callable[[dict], bool]] = field(default_factory=dict)
    refuses: Mapping[str, Callable[[dict], str | None]] = field(default_factory=dict)
    stamp: Callable[[], dict[str, Any]] = _pinned
    known: KnownAddresses | None = None
    facts: Callable[[], str] = lambda: ""
    # One set of grants for the life of the process, shared by every wrapper built from this.
    grants: SessionGrants = field(default_factory=SessionGrants)
    prior_dispatch: PriorDispatchCheck | None = None
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
            harmless=self.harmless,
            refuses=self.refuses,
            stamp=self.stamp,
            known=self.known,
            facts=self.facts,
            grants=self.grants,
            prior_dispatch=self.prior_dispatch,
        )
