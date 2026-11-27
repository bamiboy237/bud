"""Exercise Bud's provider adapters over HTTP without contacting any provider."""

import json
from base64 import b64decode
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from pydantic_ai import Agent, Conversation, ConversationTypeAdapter
from pydantic_ai.messages import (
    BinaryContent,
    CompactionPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials

from bud.config import Settings
from bud.gateway import router
from bud.gateway.chatgpt import ChatGPTStore
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, load_registry

OPENAI = "openai:gpt-6-luna"
CHATGPT = "openai-chatgpt:gpt-6-luna"
DEEPSEEK = "deepseek:deepseek-flash"
MISTRAL = "mistral:mistral-large-latest"
LONG_CALL_ID = "call_eada9bcd36c04b156c7241610f6e2127"
REASONING = "Look up the order before answering."
SUMMARY = "The owner asked to track order A-1042."
ENCRYPTED_THINKING = "opaque-openai-reasoning"
ENCRYPTED_COMPACTION = "opaque-openai-compaction"
PNG = BinaryContent(
    data=b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+jBFsAAAAASUVORK5CYII="
    ),
    media_type="image/png",
)
PDF = BinaryContent(data=b"%PDF-1.4\n%%EOF\n", media_type="application/pdf")
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*httpx.AsyncClient.*support for OpenAI-compatible providers is deprecated"
)


@pytest.fixture
async def wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[router.Gateway, list[tuple[str, dict[str, Any]]]]]:
    requests: list[tuple[str, dict[str, Any]]] = []

    def respond(path: str, body: dict[str, Any]) -> dict[str, Any]:
        requests.append((path, body))
        if path.endswith("/chat/completions"):
            result = {
                "id": "chatcmpl_mock",
                "object": "chat.completion",
                "created": 1,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Delivered."},
                    }
                ],
            }
        else:
            assert path.endswith("/responses")
            result = {
                "id": "resp_mock",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "tools": [],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "output": [
                    {
                        "type": "message",
                        "id": "msg_mock",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "Delivered.", "annotations": []}
                        ],
                    }
                ],
            }
        return result | {"model": body["model"]}

    def api_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json=respond(request.url.path, json.loads(request.content)))

    def chatgpt_response(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-plan-token"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["store"] is False
        completed = respond(request.url.path, body)
        events = [
            {"type": "response.created", "response": completed | {"output": []}},
            {
                "type": "response.output_text.delta",
                "item_id": "msg_mock",
                "output_index": 0,
                "content_index": 0,
                "delta": "Delivered.",
            },
            {"type": "response.completed", "response": completed},
        ]
        return httpx2.Response(
            200,
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            headers={"Content-Type": "text/event-stream"},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(api_response)) as client,
        httpx2.AsyncClient(transport=httpx2.MockTransport(chatgpt_response)) as chatgpt_client,
    ):
        for name in ("OpenAIProvider", "DeepSeekProvider", "EndpointProvider"):
            monkeypatch.setattr(router, name, partial(getattr(router, name), http_client=client))
        monkeypatch.setattr(
            router,
            "OpenAIChatGPTProvider",
            partial(router.OpenAIChatGPTProvider, http_client=chatgpt_client),
        )
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY"):
            monkeypatch.setenv(name, "test-key")
        registry = load_registry()
        registry.models.append(
            ModelSpec(
                provider="openai-chatgpt", name="gpt-6-luna", tier="fast", billing="subscription"
            )
        )
        store = ChatGPTStore(tmp_path)
        await store.save(
            OpenAIChatGPTCredentials(
                client_id="test-client",
                access_token="test-plan-token",
                refresh_token="test-refresh-token",
                scope="chatgpt.tokens.use.direct",
            )
        )
        gateway = router.Gateway(
            registry,
            Ledger(tmp_path / "ledger.sqlite"),
            router.provider_model_factory(Settings(), store),
            chatgpt=store,
        )
        try:
            with override_allow_model_requests(True):
                yield gateway, requests
        finally:
            gateway.close()


def saved_history(source: str, call_id: str) -> Conversation:
    provider, model = source.split(":", 1)
    is_responses = source in {OPENAI, CHATGPT}
    # Bud's Mistral endpoint uses the generic OpenAI provider name.
    provider_name = "openai" if source == MISTRAL else provider
    conversation = Conversation(
        messages=[
            ModelRequest(parts=[CompactionPart(content=SUMMARY)]),
            ModelRequest(parts=[UserPromptPart(["Check order A-1042.", PNG, PDF])]),
            ModelResponse(
                parts=[
                    ThinkingPart(
                        REASONING,
                        id="rs_previous" if is_responses else "reasoning_content",
                        signature=ENCRYPTED_THINKING if is_responses else None,
                        provider_name=provider_name,
                    ),
                    ToolCallPart(
                        "lookup_order",
                        {"order": "A-1042"},
                        tool_call_id=call_id,
                        id="fc_previous" if is_responses else None,
                        provider_name=provider_name,
                    ),
                ],
                provider_name=provider_name,
                provider_url={
                    OPENAI: "https://api.openai.com/v1/",
                    CHATGPT: "https://api.openai.com/v1/",
                    DEEPSEEK: "https://api.deepseek.com/",
                    MISTRAL: "https://api.mistral.ai/v1/",
                }[source],
                provider_response_id="resp_previous" if is_responses else "chatcmpl_previous",
                model_name=model,
            ),
            ModelRequest(
                parts=[ToolReturnPart("lookup_order", "In transit.", tool_call_id=call_id)]
            ),
            ModelResponse(parts=[TextPart("Order A-1042 is in transit.")]),
        ]
    )
    if is_responses:
        conversation.messages.append(
            ModelResponse(
                parts=[
                    CompactionPart(
                        id="cmp_previous",
                        provider_name=provider_name,
                        provider_details={"encrypted_content": ENCRYPTED_COMPACTION},
                    )
                ],
                provider_name=provider_name,
                provider_response_id="resp_compacted",
                model_name=model,
            )
        )

    return ConversationTypeAdapter.validate_json(ConversationTypeAdapter.dump_json(conversation))


async def switch_model(gateway: router.Gateway, source: str, target: str, call_id: str) -> None:
    agent = Agent(gateway.model("fast", selected=target))

    @agent.tool_plain
    def lookup_order(order: str) -> str:
        raise AssertionError(f"Settled tool call ran again for {order}")

    result = await agent.run(
        "What's the current status?", conversation=saved_history(source, call_id)
    )
    assert result.output == "Delivered."
    assert result.new_messages()[-1].metadata == {"bud_model_id": target}


@pytest.mark.parametrize("source", [OPENAI, CHATGPT])
@pytest.mark.parametrize("target", [DEEPSEEK, MISTRAL])
async def test_responses_history_switches_to_chat(
    wire: tuple[router.Gateway, list[tuple[str, dict[str, Any]]]], source: str, target: str
) -> None:
    gateway, requests = wire
    await switch_model(gateway, source, target, LONG_CALL_ID)

    [(path, body)] = requests
    assert path.endswith("/chat/completions")
    assert body["model"] == target.split(":", 1)[1]
    messages = body["messages"]
    [assistant] = [message for message in messages if message.get("tool_calls")]
    [call] = assistant["tool_calls"]
    [tool] = [message for message in messages if message["role"] == "tool"]
    assert call["id"] == tool["tool_call_id"] == LONG_CALL_ID
    assert call["function"]["name"] == "lookup_order"
    assert json.loads(call["function"]["arguments"]) == {"order": "A-1042"}
    assert tool["content"] == "In transit."
    assert messages.index(tool) == messages.index(assistant) + 1
    if target == DEEPSEEK:
        assert assistant["reasoning_content"] == REASONING

    [parts] = [message["content"] for message in messages if isinstance(message["content"], list)]
    assert {"type": "image_url", "image_url": {"url": PNG.data_uri}} in parts
    assert {
        "type": "file",
        "file": {"file_data": PDF.data_uri, "filename": "filename.pdf"},
    } in parts
    serialized = json.dumps(body)
    assert any(
        message["role"] == "user"
        and str(message["content"]).startswith("<conversation_summary>\n")
        and SUMMARY in message["content"]
        for message in messages
    )
    assert REASONING in serialized
    for opaque in (ENCRYPTED_THINKING, ENCRYPTED_COMPACTION, "cmp_previous", "fc_previous"):
        assert opaque not in serialized
    if source == OPENAI and target == MISTRAL and "rs_previous" in assistant:
        assert assistant["rs_previous"] == REASONING
        pytest.xfail("Generic Mistral provider misreads the OpenAI reasoning ID as a JSON field")
    assert "rs_previous" not in serialized


@pytest.mark.parametrize("target", [OPENAI, CHATGPT])
@pytest.mark.parametrize(("source", "call_id"), [(DEEPSEEK, LONG_CALL_ID), (MISTRAL, "VvvODy9mT")])
async def test_chat_history_switches_to_responses(
    wire: tuple[router.Gateway, list[tuple[str, dict[str, Any]]]],
    source: str,
    call_id: str,
    target: str,
) -> None:
    gateway, requests = wire
    await switch_model(gateway, source, target, call_id)

    [(path, body)] = requests
    assert path.endswith("/responses")
    assert body["model"] == target.split(":", 1)[1]
    items = body["input"]
    [call] = [item for item in items if item.get("type") == "function_call"]
    [output] = [item for item in items if item.get("type") == "function_call_output"]
    assert call["call_id"] == output["call_id"] == call_id
    assert call["name"] == "lookup_order"
    assert json.loads(call["arguments"]) == {"order": "A-1042"}
    assert output["output"] == "In transit."
    assert items.index(output) == items.index(call) + 1

    parts = [
        part
        for item in items
        if item.get("role") == "user" and isinstance(item["content"], list)
        for part in item["content"]
    ]
    assert {"type": "input_image", "image_url": PNG.data_uri, "detail": "auto"} in parts
    assert {"type": "input_file", "file_data": PDF.data_uri, "filename": "filename.pdf"} in parts
    serialized = json.dumps(body)
    assert any(
        item.get("role") == "user"
        and str(item["content"]).startswith("<conversation_summary>\n")
        and SUMMARY in item["content"]
        for item in items
    )
    assert REASONING in serialized
    assert "previous_response_id" not in body
    assert "chatcmpl_previous" not in serialized
    assert "reasoning_content" not in serialized
    assert all(item.get("type") != "reasoning" for item in items)
