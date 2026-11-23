"""Create and review scheduled routines from the owning conversation."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from bud.core.projection import RoutineView
from bud.core.routines import RoutineDefinition, RoutineError, RoutineStore

CREATE_ROUTINE = "create_routine"
LIST_ROUTINES = "list_routines"


def _owner_chat(ctx: RunContext[None]) -> bool:
    metadata = ctx.metadata or {}
    return bool(
        metadata.get("bud_session_id")
        and metadata.get("bud_task_id") is None
        and metadata.get("bud_occurrence_id") is None
        and not metadata.get("bud_revision_pending")
    )


@dataclass(kw_only=True)
class RoutineChat(AbstractCapability[None]):
    store: RoutineStore
    create: Callable[..., RoutineDefinition]
    id: str | None = "bud_chat_routines"

    def get_toolset(self):
        return FunctionToolset([self.create_routine, self.list_routines])

    async def prepare_tools(
        self, ctx: RunContext[None], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        if not _owner_chat(ctx):
            return [tool for tool in tool_defs if tool.name not in {CREATE_ROUTINE, LIST_ROUTINES}]
        if not (ctx.metadata or {}).get("bud_request"):
            return [tool for tool in tool_defs if tool.name != CREATE_ROUTINE]
        return tool_defs

    def create_routine(
        self,
        ctx: RunContext[None],
        title: str,
        skill: str,
        prompt: str,
        cron: str,
        timezone: str,
    ) -> RoutineView:
        """Propose a recurring job only when the owner explicitly asks for one.

        Ask about any missing schedule, time zone, or objective first. Use list_skills
        to find an enabled skill if needed. The owner approves this exact proposal before
        it starts. Results return to this chat. Explain the schedule in ordinary language.

        Args:
            title: Short name for this routine.
            skill: Name of the enabled skill to run.
            prompt: What Bud should do on each scheduled run.
            cron: Five-field cron expression in the specified time zone.
            timezone: IANA time zone, for example America/Chicago.
        """
        metadata = ctx.metadata or {}
        owner_request = metadata.get("bud_request")
        if not _owner_chat(ctx) or not isinstance(owner_request, str) or not owner_request.strip():
            raise ModelRetry("Creating a routine requires a request in the owner's chat.")
        if not ctx.tool_call_approved:
            raise ModelRetry("Ask for approval before creating the routine.")
        feed_id = metadata["bud_session_id"]
        try:
            routine = self.create(
                feed_id=feed_id,
                title=title,
                skill=skill,
                prompt=prompt,
                cron=cron,
                timezone=timezone,
                source_id=uuid5(NAMESPACE_URL, f"bud:routine:{ctx.run_id}:{ctx.tool_call_id}").hex,
                owner_request=owner_request,
            )
        except RoutineError as exc:
            raise ModelRetry(str(exc)) from None
        return self.store.view(routine)

    def list_routines(self, ctx: RunContext[None]) -> list[RoutineView]:
        """Review this chat's routines, including their schedule, next run, and status."""
        if not _owner_chat(ctx):
            raise ModelRetry("Review routines from the owner's chat.")
        return self.store.views((ctx.metadata or {})["bud_session_id"])
