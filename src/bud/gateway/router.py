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
from pydantic_ai.profiles.openai import OpenAIModelProfile
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


class EndpointProvider(OpenAIProvider):
    @staticmethod
    def model_profile(model_name: str) -> OpenAIModelProfile:
        """A custom endpoint's capabilities come from its configuration, not its model name."""
        return OpenAIModelProfile(
            context_window=None,
            supports_tools=False,
            supported_native_tools=frozenset(),
            supports_json_schema_output=False,
            supports_json_object_output=False,
            supports_image_input=False,
            supports_document_input=False,
            supports_audio_file_input=False,
            openai_supports_strict_tool_definition=False,
            tool_deferral_mode="none",
            tool_addition_mode="with_definitions",
        )


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
        key = (spec.provider, spec.base_url)
        if key not in providers:
            if spec.base_url:
                secret = settings.model_api_keys.get(spec.provider)
                if spec.billing == "api" and secret is None:
                    return None
                providers[key] = EndpointProvider(
                    base_url=spec.base_url,
                    api_key=secret.get_secret_value() if secret else "local-no-key",
                )
            elif spec.provider == "openai-chatgpt":
                if not chatgpt.connected:
                    return None
                chatgpt.acquire()
                providers[key] = OpenAIChatGPTProvider(credential_source=chatgpt)
            elif spec.provider == "deepseek":
                if not settings.deepseek_api_key:
                    return None
                providers[key] = DeepSeekProvider(
                    api_key=settings.deepseek_api_key.get_secret_value()
                )
            elif spec.provider == "openai":
                if not settings.openai_api_key:
                    return None
                providers[key] = OpenAIProvider(api_key=settings.openai_api_key.get_secret_value())
            elif spec.provider == "meta":
                if not settings.muse_api_key:
                    return None
                providers[key] = MetaProvider(api_key=settings.muse_api_key.get_secret_value())
            else:
                raise ValueError(f"unknown provider in registry: {spec.provider}")
        protocol = spec.protocol or ("chat" if spec.provider == "deepseek" else "responses")
        model_type = OpenAIChatModel if protocol == "chat" else OpenAIResponsesModel
        options = dict(model_settings)
        if spec.max_tokens is not None:
            options["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            options["openai_reasoning_effort"] = spec.reasoning_effort
        profile = None
        if spec.supports_tools is not None or spec.context_window is not None:
            profile = profile or {}
            if spec.supports_tools is not None:
                profile["supports_tools"] = spec.supports_tools
            if spec.context_window is not None:
                profile["context_window"] = spec.context_window
        return model_type(spec.name, provider=providers[key], settings=options, profile=profile)

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
        fallback: bool = True,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.build_model = build_model
        self.selected_model = selected_model
        self.chatgpt = chatgpt
        self.fallback = fallback

    def close(self) -> None:
        if self.chatgpt is not None:
            self.chatgpt.close()

    def model(
        self, tier: Tier, session_id: str | None = None, *, require_tools: bool = False
    ) -> Model:
        if self.selected_model:
            specs = [spec for spec in self.registry.models if spec.id == self.selected_model][:1]
        else:
            specs = self.registry.tier(tier)
        chain: list[TrackedModel] = []
        for spec in specs:
            if (
                chain
                and (spec.billing == "local" or chain[0].spec.billing == "local")
                and spec.base_url != chain[0].spec.base_url
            ):
                break
            model = self.build_model(spec)
            if model is not None:
                if require_tools and not model.profile.get("supports_tools", True):
                    raise NoModelAvailable(
                        f"{spec.id} does not support tools. Select a tool-capable model."
                    )
                chain.append(TrackedModel(model, spec, self.ledger, session_id))
                if not self.fallback or self.selected_model:
                    break
        if not chain:
            target = self.selected_model or f"tier {tier!r}"
            raise NoModelAvailable(
                f"No configured model for {target}. Check MODELS_FILE, model keys or "
                "run `bud login chatgpt`."
            )
        if len(chain) == 1:
            return chain[0]
        return FallbackModel(
            *chain,
            fallback_on=partial(
                should_fall_back, subscription=chain[0].spec.billing == "subscription"
            ),
        )
