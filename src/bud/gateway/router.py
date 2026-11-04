"""Provider clients and per-request fallback; tools never run inside a retry."""

from collections.abc import Callable
from functools import partial

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
from pydantic_ai.providers.meta import MetaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTProvider
from pydantic_ai.settings import ModelSettings

from bud.config import Settings
from bud.gateway.chatgpt import ChatGPTStore
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry, Tier
from bud.gateway.tracked import TrackedModel

REQUEST_TIMEOUT_SECONDS = 120
ModelFactory = Callable[[ModelSpec], Model | None]


class NoModelAvailable(Exception):
    pass


def should_fall_back(exc: Exception, *, subscription: bool = False) -> bool:
    """Plan routes only fall back on availability; API routes retain rate/quota fallback."""
    code = getattr(exc, "provider_error_code", None) or ""
    if code.startswith("subscription_sharing_"):
        return False
    if isinstance(exc, ModelHTTPError):
        return exc.status_code >= 500 or (not subscription and exc.status_code in (402, 429))
    if isinstance(exc, (ModelRateLimitError, ModelQuotaExceededError)):
        return not subscription
    return isinstance(
        exc, (ModelConnectionError, ModelRequestTimeout, ModelUnavailableError, ModelServerError)
    )


def provider_model_factory(settings: Settings, chatgpt: ChatGPTStore) -> ModelFactory:
    model_settings = ModelSettings(timeout=REQUEST_TIMEOUT_SECONDS)
    providers = {}

    def build(spec: ModelSpec) -> Model | None:
        if spec.provider not in providers:
            if spec.provider == "openai-chatgpt":
                if not chatgpt.connected:
                    return None
                chatgpt.acquire()
                providers[spec.provider] = OpenAIChatGPTProvider(credential_source=chatgpt)
            elif spec.provider == "deepseek":
                if not settings.deepseek_api_key:
                    return None
                providers[spec.provider] = DeepSeekProvider(
                    api_key=settings.deepseek_api_key.get_secret_value()
                )
            elif spec.provider == "openai":
                if not settings.openai_api_key:
                    return None
                providers[spec.provider] = OpenAIProvider(
                    api_key=settings.openai_api_key.get_secret_value()
                )
            elif spec.provider == "meta":
                if not settings.muse_api_key:
                    return None
                providers[spec.provider] = MetaProvider(
                    api_key=settings.muse_api_key.get_secret_value()
                )
            else:
                raise ValueError(f"unknown provider in registry: {spec.provider}")
        model_type = OpenAIChatModel if spec.provider == "deepseek" else OpenAIResponsesModel
        return model_type(spec.name, provider=providers[spec.provider], settings=model_settings)

    return build


class Gateway:
    """The only path from bud to a model provider."""

    def __init__(
        self,
        registry: Registry,
        ledger: Ledger,
        build_model: ModelFactory,
        *,
        selected_model: str | None = None,
        chatgpt: ChatGPTStore | None = None,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.build_model = build_model
        self.selected_model = selected_model
        self.chatgpt = chatgpt

    def close(self) -> None:
        if self.chatgpt is not None:
            self.chatgpt.close()

    def model(self, tier: Tier, session_id: str | None = None) -> Model:
        if self.selected_model:
            specs = [spec for spec in self.registry.models if spec.id == self.selected_model][:1]
        else:
            specs = self.registry.tier(tier)
        chain: list[TrackedModel] = []
        for spec in specs:
            model = self.build_model(spec)
            if model is not None:
                chain.append(TrackedModel(model, spec, self.ledger, session_id))
        if not chain:
            target = self.selected_model or f"tier {tier!r}"
            raise NoModelAvailable(
                f"No model credentials for {target}. Run `bud login chatgpt` or set an API key."
            )
        if len(chain) == 1:
            return chain[0]
        return FallbackModel(
            *chain,
            fallback_on=partial(
                should_fall_back, subscription=chain[0].spec.billing == "subscription"
            ),
        )
