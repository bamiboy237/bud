"""The harness ConversationSearch tool reads bud's event log, scoped to one conversation."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from tests.runner_support import reply, running_bud


def owner_words(messages: list[ModelMessage]) -> str:
    request = messages[-1]
    assert isinstance(request, ModelRequest)
    return " ".join(
        part.content
        for part in request.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    said = owner_words(messages)
    if "door code" in said:
        return reply("The storage unit door code is CEDAR-7731.")
    if "Wi-Fi" in said:
        return reply("The office Wi-Fi password is ZEPHYR-2048.")
    if said.startswith("Search for "):
        query = said.removeprefix("Search for ").split()[0]
        call = ToolCallPart("search_conversation_history", {"query": query}, tool_call_id="q1")
        return ModelResponse(parts=[call])
    return reply("Done.")


async def search(bud, session: str, query: str, message_id: str) -> str:
    bud.runner.start(session, f"Search for {query} please.", message_id)
    await bud.settle()
    results = [e.data for e in bud.log.read(session) if e.type == "tool.result"]
    assert results[-1]["tool"] == "search_conversation_history"
    assert results[-1]["outcome"] == "success"
    return results[-1]["result"]


async def test_search_finds_this_conversations_earlier_turns_only(tmp_path):
    async with running_bud(tmp_path, model) as bud:
        for session in ("owner", "other"):
            bud.log.append(session, "session.created")
        bud.runner.start("owner", "What is the door code?", "m1")
        await bud.settle()
        bud.runner.start("other", "What is the Wi-Fi password?", "m2")
        await bud.settle()

        found = await search(bud, "owner", "CEDAR-7731", "m3")
        hidden = await search(bud, "owner", "ZEPHYR-2048", "m4")

    # The tool echoes the query, so check for the saved sentences, not the codes.
    assert "storage unit door code" in found
    assert "office Wi-Fi password" not in hidden
    assert hidden.startswith("No matches")
