import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from bud.config import Settings
from bud.harness.verify import build_verifier


async def test_verifier_uses_decisions_until_auth_fails(monkeypatch):
    requests = []
    status = 200

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "answers": [
                    {
                        "name": "item_0",
                        "type": "choice",
                        "probabilities": [{"value": "present", "probability": 1.0}],
                    }
                ]
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    verifier = build_verifier(
        Settings(approval_review=True, openai_api_key=SecretStr("offline-key")),
        model_provider=lambda: FunctionModel(
            lambda *_: ModelResponse(parts=[TextPart('{"item_0":"missing"}')])
        ),
    )
    assert verifier is not None
    try:
        assert await verifier.check(["a link"], "a draft", []) == {"a link": True}
        status = 403
        for _ in range(2):
            assert await verifier.check(["a link"], "a draft", []) == {"a link": False}
        assert len(requests) == 2
    finally:
        await verifier.close()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"item_0":"present","item_1":"missing"}', {"a link": True, "a date": False}),
        ('{"item_0":"present"}', None),
        ('{"item_0":"present","item_1":"absent"}', None),
        ('{"item_0":"present","item_1":"missing","item_2":"present"}', None),
    ],
)
async def test_verifier_without_key_requires_exact_gateway_answers(answer, expected):
    verifier = build_verifier(
        Settings(approval_review=True),
        model_provider=lambda: FunctionModel(lambda *_: ModelResponse(parts=[TextPart(answer)])),
    )
    assert verifier is not None
    try:
        assert await verifier.check(["a link", "a date"], "a draft", []) == expected
    finally:
        await verifier.close()
