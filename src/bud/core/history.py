"""bud's event log as the corpus of the harness `ConversationSearch` tool.

`ConversationSearch` reads persisted runs through a `HistorySource`. bud already saves
each run's own messages in its `run.waiting` and `run.completed` events, so this adapter
replays them instead of keeping a second copy with `StepPersistence`.
"""

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai_harness import ConversationSearch
from pydantic_ai_harness.step_persistence import RunRecord

from bud.core.log import EventLog


class EventLogHistory:
    def __init__(self, log: EventLog) -> None:
        self.log = log

    async def list_runs(self) -> list[RunRecord]:
        return [
            RunRecord(run_id=run_id, conversation_id=session_id, started_at=started_at)
            for session_id, run_id, started_at in self.log.saved_runs()
        ]

    async def run_history(self, *, run_id: str) -> list[ModelMessage]:
        return ModelMessagesTypeAdapter.validate_python(self.log.run_messages(run_id))


def conversation_search(log: EventLog) -> ConversationSearch[None]:
    """Search this conversation's earlier runs, including turns compaction dropped."""
    return ConversationSearch(EventLogHistory(log), scope="conversation")
