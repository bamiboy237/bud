"""Routes a tier to an ordered chain of provider models with fallback.
`Gateway.model` returns a Pydantic AI `FallbackModel` over tracked models, and
`provider_model_factory` builds the real provider clients.
"""

from collections.abc import Callable

from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from bud.config import Settings
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry, Tier
from bud.gateway.tracked import TrackedModel

REQUEST_TIMEOUT_SECONDS = 120

ModelFactory = Callable[[ModelSpec], Model | None]

FALLBACK_ERRORS = (
    ModelRateLimitError,
    ModelQuotaExceededError,
    ModelUnavailableError,
    ModelServerError,
    ModelConnectionError,
    ModelRequestTimeout,
)


class NoModelAvailable(Exception):
    pass


def should_fall_back(exc: Exception) -> bool:
    """A bad request fails on every provider, so it never falls back."""
    if isinstance(exc, FALLBACK_ERRORS):
        return True
    return isinstance(exc, ModelHTTPError) and (
        exc.status_code in (402, 429) or exc.status_code >= 500
    )


def provider_model_factory(settings: Settings) -> ModelFactory:
    model_settings = ModelSettings(timeout=REQUEST_TIMEOUT_SECONDS)

    def build(spec: ModelSpec) -> Model | None:
        if spec.provider == "deepseek":
            if settings.deepseek_api_key is None:
                return None
            provider = DeepSeekProvider(api_key=settings.deepseek_api_key.get_secret_value())
            return OpenAIChatModel(spec.name, provider=provider, settings=model_settings)
        if spec.provider == "openai":
            if settings.openai_api_key is None:
                return None
            provider = OpenAIProvider(api_key=settings.openai_api_key.get_secret_value())
            return OpenAIResponsesModel(spec.name, provider=provider, settings=model_settings)
        raise ValueError(f"unknown provider in registry: {spec.provider}")

    return build


class Gateway:
    """The only path from bud to a model provider."""

    def __init__(self, registry: Registry, ledger: Ledger, build_model: ModelFactory) -> None:
        self.registry = registry
        self.ledger = ledger
        self.build_model = build_model

    def model(self, tier: Tier, session_id: str | None = None) -> Model:
        """A Pydantic AI model for a tier.

        Fallback happens per model request, so a failure late in an agent run retries that one
        request and never repeats tool calls that already ran.
        """
        chain: list[Model] = []
        for spec in self.registry.tier(tier):
            model = self.build_model(spec)
            if model is not None:
                chain.append(TrackedModel(model, spec, self.ledger, session_id))
        if not chain:
            raise NoModelAvailable(f"no model with credentials in tier {tier!r}")
        return FallbackModel(*chain, fallback_on=should_fall_back)
