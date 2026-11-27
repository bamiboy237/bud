from unittest.mock import AsyncMock, MagicMock

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
    UserError,
)
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.compaction import resolve_context_window

from bud.config import get_settings
from bud.gateway import build_gateway, chatgpt
from bud.gateway.errors import (
    NoModelAvailable,
    WorkingContextBudgetError,
    error_kind,
    error_summary,
)
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway, provider_model_factory


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


async def test_chatgpt_catalog_preserves_and_resolves_context_windows(monkeypatch):
    models = [
        {"slug": "gpt-6.1-terra", "context_window": 272_000, "max_tokens": 12_000},
        {"slug": "catalog-length", "context_window": True, "max_context_length": 160_000},
        {"slug": "catalog-input-tokens", "context_window": "64000", "max_input_tokens": 90_000},
        {"slug": "catalog-output-only", "max_tokens": 32_768},
        {"slug": "gpt-5.2", "context_window": -1},
        {"slug": "gpt-6.2-sol", "max_tokens": 0},
        {"slug": "unlisted-family", "max_context_length": 1.5},
    ]
    client = MagicMock()
    client.get = AsyncMock(
        return_value={"models": [dict(row, visibility="list") for row in models]}
    )
    provider = MagicMock(client=client)
    monkeypatch.setattr(chatgpt, "OpenAIChatGPTProvider", lambda **_: provider)
    settings = get_settings()
    store = chatgpt.ChatGPTStore(settings.data_dir)

    refreshed = await chatgpt.refresh_models(store)
    assert store.state()["models"] == refreshed
    assert [row.get("context_window") for row in refreshed] == [272_000, 160_000, 90_000] + [
        None
    ] * 4
    store.path.touch()

    priced_window = resolve_context_window("openai-chatgpt:gpt-5.2")
    assert priced_window is not None
    gateway = build_gateway(settings)
    try:
        assert {
            spec.name: spec.context_window
            for spec in gateway.registry.models
            if spec.provider == "openai-chatgpt"
        } == {
            "gpt-6.1-terra": 272_000,
            "catalog-length": 160_000,
            "catalog-input-tokens": 90_000,
            "catalog-output-only": None,
            "gpt-5.2": priced_window,
            "gpt-6.2-sol": 200_000,
            "unlisted-family": None,
        }
        selected = gateway.model("fast", selected="openai-chatgpt:gpt-6.1-terra")
        assert selected.context_window == 272_000
        status = next(row for row in gateway.catalog() if row.id == "openai-chatgpt:gpt-6.1-terra")
        assert status.configured and status.selectable
        assert status.context_window == selected.context_window
        store.path.unlink()
        assert not gateway.selectable_models()
    finally:
        gateway.close()


@pytest.mark.parametrize(
    ("error_type", "kind", "summary"),
    [
        (UserError, "unknown", "UserError"),
        (
            WorkingContextBudgetError,
            "context_full",
            "Recent content or tool definitions exceed Bud's working context budget.",
        ),
    ],
)
def test_user_error_classification_keeps_messages_private(error_type, kind, summary):
    error = error_type("Private request contents")
    assert error_kind(error) == kind
    assert error_summary(error) == summary


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


def test_catalog_and_validation_use_the_same_routes(gateway_factory):
    make, calls, builds, _ = gateway_factory
    ready = api_spec("ready")
    text_only = api_spec("text-only")
    unavailable = api_spec("unavailable")
    gateway = make(
        [ready, text_only, ready.model_copy(update={"tier": "frontier"}), unavailable],
        unavailable={unavailable.id},
        tool_less={text_only.id},
    )

    catalog = gateway.catalog()
    assert builds == [ready.id, text_only.id, unavailable.id]
    assert [row.id for row in catalog] == builds
    assert [(row.configured, row.supports_tools, row.selectable) for row in catalog] == [
        (True, True, True),
        (True, False, False),
        (False, False, False),
    ]
    assert gateway.selectable_models() == [ready.id]
    assert gateway.selectable_models(require_tools=False) == [ready.id, text_only.id]
    assert gateway.validate_model(ready.id) == ready
    assert gateway.validate_model(text_only.id, require_tools=False) == text_only
    for model_id, reason in (
        (text_only.id, "does not support tools"),
        (unavailable.id, "not configured"),
        ("unknown", "Unknown model"),
    ):
        with pytest.raises(NoModelAvailable, match=reason):
            gateway.validate_model(model_id)
    assert not calls


def test_model_build_error_is_reported_without_provider_details(gateway_factory):
    _, _, _, ledger = gateway_factory
    spec = api_spec("failed")

    def build(_):
        raise ModelHTTPError(401, spec.id, body="private provider detail")

    gateway = Gateway(Registry(models=[spec]), ledger, build)
    assert not gateway.catalog()[0].configured
    with pytest.raises(NoModelAvailable, match="not configured") as error:
        gateway.validate_model(spec.id)
    assert "private provider detail" not in str(error.value)


def test_cached_local_provider_does_not_configure_api_route(gateway_factory):
    _, _, _, ledger = gateway_factory
    settings = get_settings()
    local = local_spec("local-model")
    api = local.model_copy(update={"name": "api-model", "billing": "api"})
    gateway = Gateway(
        Registry(models=[local, api]),
        ledger,
        provider_model_factory(settings, chatgpt.ChatGPTStore(settings.data_dir)),
    )

    assert gateway.selectable_models() == [local.id]
    with pytest.raises(NoModelAvailable, match="not configured"):
        gateway.validate_model(api.id)


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
