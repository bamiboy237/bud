import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelConnectionError,
    ModelContextWindowExceededError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
)
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from bud.gateway.errors import NoModelAvailable
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway


def api_spec(name, **changes):
    return ModelSpec.model_validate(
        {
            "provider": "openai",
            "name": name,
            "tier": "fast",
            "input": 2,
            "output": 4,
            "cached": 1,
        }
        | changes
    )


def local_spec(name, endpoint="http://localhost:8000/v1"):
    return api_spec(
        name,
        provider="local",
        billing="local",
        base_url=endpoint,
        protocol="chat",
        supports_tools=True,
    )


def subscription_spec():
    return api_spec("plan", provider="openai-chatgpt", billing="subscription")


@pytest.fixture
def gateway_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    calls = []
    builds = []
    ledger = Ledger(tmp_path / "ledger.sqlite")

    def make(specs, failures=None, unavailable=(), tool_less=(), **options):
        failures = failures or {}

        def build(spec):
            builds.append(spec.id)
            if spec.id in unavailable:
                return None

            async def request(messages, info):
                calls.append(spec.id)
                if spec.id in failures:
                    raise failures[spec.id]
                return ModelResponse(
                    parts=[TextPart(f"response from {spec.id}")],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                )

            async def stream_request(messages, info):
                response = await request(messages, info)
                yield response.parts[0].content

            return FunctionModel(
                request,
                stream_function=stream_request,
                profile={"supports_tools": spec.id not in tool_less},
            )

        gateway = Gateway(Registry(models=specs), ledger, build, **options)
        return gateway

    return make, calls, builds, ledger


async def ask(gateway, *, streaming=False, **selection):
    model = gateway.model("fast", session_id="session-1", **selection)
    messages = [ModelRequest(parts=[UserPromptPart("offline question")])]
    if streaming:
        async with model.request_stream(messages, None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass
            return stream.get().parts[0].content
    response = await model.request(
        messages,
        None,
        ModelRequestParameters(),
    )
    return response.parts[0].content


@pytest.mark.parametrize(
    "error",
    [
        ModelHTTPError(500, "primary"),
        ModelHTTPError(503, "primary"),
        ModelHTTPError(402, "primary"),
        ModelHTTPError(429, "primary"),
        ModelConnectionError("primary", "offline unavailable"),
        ModelRequestTimeout("primary", 1),
        ModelUnavailableError("primary", "offline unavailable"),
        ModelServerError("primary", "offline unavailable"),
        ModelRateLimitError("primary", "offline rate limit"),
        ModelQuotaExceededError("primary", "offline quota"),
    ],
)
async def test_api_availability_rate_and_quota_failures_fall_back(gateway_factory, error):
    make, calls, _, ledger = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    gateway = make([primary, backup], {primary.id: error})
    assert await ask(gateway) == f"response from {backup.id}"
    assert calls == [primary.id, backup.id]
    assert ledger.session_models("session-1") == [
        {"model": backup.id, "billing": "api"},
        {"model": primary.id, "billing": "api"},
    ]
    assert ledger.session_totals("session-1") == (pytest.approx(0.00004), 15)
    assert sum(row.failed for row in ledger.costs_by_day_and_model()) == 1


@pytest.mark.parametrize(
    "error",
    [
        ModelHTTPError(400, "primary"),
        ModelHTTPError(401, "primary"),
        ModelHTTPError(403, "primary"),
        ModelHTTPError(404, "primary"),
        ModelHTTPError(408, "primary"),
        ContentFilterError("offline refusal"),
        ModelContextWindowExceededError("primary", "offline context full"),
        ValueError("invalid request"),
    ],
)
async def test_nonretry_errors_do_not_call_backup(gateway_factory, error):
    make, calls, _, ledger = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    gateway = make([primary, backup], {primary.id: error})
    with pytest.raises(type(error)) as caught:
        await ask(gateway)
    assert caught.value is error
    assert calls == [primary.id]
    assert ledger.session_totals("session-1") == (0, 0)


@pytest.mark.parametrize(
    "error",
    [
        ModelHTTPError(402, "plan"),
        ModelHTTPError(429, "plan"),
        ModelRateLimitError("plan", "offline rate limit"),
        ModelQuotaExceededError("plan", "offline quota"),
        ModelServerError(
            "plan",
            "offline sharing error",
            provider_error_code="subscription_sharing_lease_lost",
        ),
    ],
)
async def test_subscription_rate_quota_and_sharing_errors_do_not_fall_back(gateway_factory, error):
    make, calls, _, _ = gateway_factory
    primary, backup = subscription_spec(), api_spec("backup")
    gateway = make([primary, backup], {primary.id: error})
    with pytest.raises(type(error)):
        await ask(gateway)
    assert calls == [primary.id]


async def test_subscription_availability_error_can_fall_back_to_api(gateway_factory):
    make, calls, _, ledger = gateway_factory
    primary, backup = subscription_spec(), api_spec("backup")
    gateway = make([primary, backup], {primary.id: ModelHTTPError(503, "plan")})
    assert await ask(gateway) == f"response from {backup.id}"
    assert calls == [primary.id, backup.id]
    assert {row.billing for row in ledger.costs_by_day_and_model()} == {"subscription", "api"}


@pytest.mark.parametrize("pin", ["gateway", "request"])
async def test_pinned_model_never_falls_back(gateway_factory, pin):
    make, calls, builds, _ = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    options = {"selected_model": primary.id} if pin == "gateway" else {}
    selection = {"selected": primary.id} if pin == "request" else {}
    gateway = make([primary, backup], {primary.id: ModelHTTPError(503, "primary")}, **options)
    with pytest.raises(ModelHTTPError):
        await ask(gateway, **selection)
    assert calls == [primary.id]
    assert builds == [primary.id]


async def test_disabled_fallback_only_calls_first_configured_model(gateway_factory):
    make, calls, builds, _ = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    gateway = make([primary, backup], {primary.id: ModelHTTPError(503, "primary")}, fallback=False)
    with pytest.raises(ModelHTTPError):
        await ask(gateway)
    assert calls == [primary.id]
    assert builds == [primary.id]


@pytest.mark.parametrize(
    "specs",
    [
        [local_spec("primary"), api_spec("backup")],
        [api_spec("primary"), local_spec("backup")],
        [
            local_spec("primary"),
            local_spec("backup", "http://localhost:9000/v1"),
        ],
    ],
)
async def test_local_routes_never_cross_endpoint_boundary(gateway_factory, specs):
    make, calls, builds, _ = gateway_factory
    primary = specs[0]
    gateway = make(specs, {primary.id: ModelHTTPError(503, "primary")})
    with pytest.raises(ModelHTTPError):
        await ask(gateway)
    assert calls == [primary.id]
    assert builds == [primary.id]


async def test_local_route_can_fall_back_within_same_endpoint(gateway_factory):
    make, calls, _, ledger = gateway_factory
    primary, backup = local_spec("primary"), local_spec("backup")
    gateway = make([primary, backup], {primary.id: ModelHTTPError(503, "primary")})
    assert await ask(gateway) == f"response from {backup.id}"
    assert calls == [primary.id, backup.id]
    assert ledger.session_totals("session-1") == (0, 15)


async def test_unconfigured_provider_is_skipped_before_request(gateway_factory):
    make, calls, _, _ = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    gateway = make([primary, backup], unavailable={primary.id})
    assert await ask(gateway) == f"response from {backup.id}"
    assert calls == [backup.id]


@pytest.mark.parametrize("selection", ["unknown", "unconfigured", "wrong_tier"])
async def test_missing_route_raises_without_request(gateway_factory, selection):
    make, calls, _, _ = gateway_factory
    primary = api_spec("primary")
    if selection == "unknown":
        gateway = make([primary], selected_model="openai:unknown")
    elif selection == "unconfigured":
        gateway = make([primary], unavailable={primary.id}, selected_model=primary.id)
    else:
        gateway = make([api_spec("primary", tier="frontier")])
    with pytest.raises(NoModelAvailable):
        await ask(gateway)
    assert calls == []


async def test_tool_requirement_does_not_silently_select_another_route(gateway_factory):
    make, calls, _, _ = gateway_factory
    primary, backup = local_spec("primary"), api_spec("backup")
    gateway = make([primary, backup], tool_less={primary.id})
    with pytest.raises(NoModelAvailable, match="does not support tools"):
        await ask(gateway, require_tools=True)
    assert calls == []


@pytest.mark.parametrize(
    "status, pinned, falls_back",
    [(503, False, True), (401, False, False), (503, True, False)],
)
async def test_streamed_requests_use_same_fallback_boundaries(
    gateway_factory, status, pinned, falls_back
):
    make, calls, _, _ = gateway_factory
    primary, backup = api_spec("primary"), api_spec("backup")
    gateway = make(
        [primary, backup],
        {primary.id: ModelHTTPError(status, "primary")},
        selected_model=primary.id if pinned else None,
    )
    if falls_back:
        assert await ask(gateway, streaming=True) == f"response from {backup.id}"
        assert calls == [primary.id, backup.id]
    else:
        with pytest.raises(ModelHTTPError):
            await ask(gateway, streaming=True)
        assert calls == [primary.id]
