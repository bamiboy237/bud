"""Provider clients and per-request fallback; tools never run inside a retry."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
    UserError,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings, OpenAIResponsesModel
from pydantic_ai.profiles.mistral import mistral_model_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.meta import MetaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTProvider
from pydantic_ai_harness.compaction import resolve_context_window

from bud.config import Settings
from bud.gateway.chatgpt import ChatGPTError, ChatGPTStore
from bud.gateway.errors import NoModelAvailable
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry, Tier
from bud.gateway.tracked import TrackedModel

REQUEST_TIMEOUT_SECONDS = 120
ModelFactory = Callable[[ModelSpec], Model | None]


def _supports_tools(model: Model) -> bool:
    return model.profile.get("supports_tools", True)


@dataclass(frozen=True)
class ModelStatus:
    spec: ModelSpec
    model: Model | None = field(repr=False)
    require_tools: bool

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def tier(self) -> Tier:
        return self.spec.tier

    @property
    def billing(self) -> str:
        return self.spec.billing

    @property
    def provider(self) -> str:
        return self.spec.provider

    @property
    def configured(self) -> bool:
        return self.model is not None

    @property
    def supports_tools(self) -> bool:
        return (
            _supports_tools(self.model)
            if self.model is not None
            else self.spec.supports_tools is True
        )

    @property
    def selectable(self) -> bool:
        return self.configured and (not self.require_tools or self.supports_tools)

    @property
    def context_window(self) -> int | None:
        return (
            resolve_context_window(self.model)
            if self.model is not None
            else self.spec.context_window
        )


class EndpointProvider(OpenAIProvider):
    @staticmethod
    def model_profile(model_name: str) -> OpenAIModelProfile:
        """A custom endpoint's capabilities come from its configuration, not its model name.

        Leading system messages are merged into one: strict servers (llama.cpp,
        some vLLM setups) reject any system message that is not the first message.
        """
        return OpenAIModelProfile(
            openai_chat_supports_multiple_system_messages=False,
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


class MistralChatProvider(EndpointProvider):
    @property
    def name(self) -> str:
        return "mistral"

    @staticmethod
    def model_profile(model_name: str) -> OpenAIModelProfile:
        return {**EndpointProvider.model_profile(model_name), **mistral_model_profile(model_name)}


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
    providers = {}

    def build(spec: ModelSpec) -> Model | None:
        if spec.provider == "openai-chatgpt" and not chatgpt.connected:
            return None
        secret = settings.model_api_keys.get(spec.provider) if spec.base_url else None
        if spec.base_url and spec.billing == "api" and secret is None:
            return None
        key = (spec.provider, spec.base_url)
        if key not in providers:
            if spec.base_url:
                endpoint = MistralChatProvider if spec.provider == "mistral" else EndpointProvider
                providers[key] = endpoint(
                    base_url=spec.base_url,
                    api_key=secret.get_secret_value() if secret else "local-no-key",
                )
            elif spec.provider == "openai-chatgpt":
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
            elif spec.provider == "mistral":
                if not settings.mistral_api_key:
                    return None
                providers[key] = MistralChatProvider(
                    base_url="https://api.mistral.ai/v1",
                    api_key=settings.mistral_api_key.get_secret_value(),
                )
            else:
                raise ValueError(f"unknown provider in registry: {spec.provider}")
        protocol = spec.protocol or (
            "chat" if spec.provider in {"deepseek", "mistral"} else "responses"
        )
        model_type = OpenAIChatModel if protocol == "chat" else OpenAIResponsesModel
        options = OpenAIChatModelSettings(timeout=REQUEST_TIMEOUT_SECONDS)
        if spec.max_tokens is not None:
            options["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            # The unified setting: pydantic-ai maps it to the closest effort this model's
            # profile accepts (or sends none), where a raw provider field could be rejected.
            options["thinking"] = (
                False if spec.reasoning_effort == "none" else spec.reasoning_effort
            )
        if spec.service_tier is not None:
            options["openai_service_tier"] = spec.service_tier
        profile = OpenAIModelProfile()
        if spec.supports_tools is not None:
            profile["supports_tools"] = spec.supports_tools
        if spec.supports_image_input is not None:
            profile["supports_image_input"] = spec.supports_image_input
        if spec.context_window is not None:
            profile["context_window"] = spec.context_window
        return model_type(
            spec.name, provider=providers[key], settings=options, profile=profile or None
        )

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
        explicit_models: frozenset[str] = frozenset(),
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.build_model = build_model
        self.selected_model = selected_model
        self.chatgpt = chatgpt
        self.fallback = fallback
        self.explicit_models = explicit_models

    def close(self, *, preserve_chatgpt: ChatGPTStore | None = None) -> None:
        if self.chatgpt is not None and self.chatgpt is not preserve_chatgpt:
            self.chatgpt.close()

    def _model_status(self, spec: ModelSpec, *, require_tools: bool) -> ModelStatus:
        try:
            model = self.build_model(spec)
        except (ChatGPTError, ModelAPIError, NoModelAvailable, OSError, UserError, ValueError):
            model = None
        return ModelStatus(spec, model, require_tools)

    def catalog(self, require_tools: bool = True) -> list[ModelStatus]:
        """Check each model ID once, in registry order."""
        statuses: dict[str, ModelStatus] = {}
        for spec in self.registry.models:
            if spec.id not in statuses:
                statuses[spec.id] = self._model_status(spec, require_tools=require_tools)
        return list(statuses.values())

    def selectable_models(self, *, require_tools: bool = True) -> list[str]:
        return [
            status.id for status in self.catalog(require_tools=require_tools) if status.selectable
        ]

    def validate_model(self, model_id: str, *, require_tools: bool = True) -> ModelSpec:
        spec = next((spec for spec in self.registry.models if spec.id == model_id), None)
        if spec is None:
            raise NoModelAvailable("Unknown model. Select a model from the available list.")
        status = self._model_status(spec, require_tools=require_tools)
        if not status.configured:
            raise NoModelAvailable(
                f"{spec.id} is not configured or available. Check its API key, "
                "ChatGPT sign-in, or endpoint settings."
            )
        if not status.selectable:
            raise NoModelAvailable(
                f"{spec.id} does not support tools. Select a tool-capable model."
            )
        return spec

    def model(
        self,
        tier: Tier,
        session_id: str | None = None,
        *,
        require_tools: bool = False,
        selected: str | None = None,
        fallback_ids: list[str] | None = None,
        workspace: Path | None = None,
    ) -> Model:
        target = selected or self.selected_model
        if target and fallback_ids:
            by_id = {spec.id: spec for spec in self.registry.tier(tier)}
            specs = (
                [
                    by_id[target],
                    *(by_id[model_id] for model_id in fallback_ids if model_id in by_id),
                ]
                if target in by_id
                else []
            )
        elif target:
            specs = [spec for spec in self.registry.models if spec.id == target][:1]
        else:
            specs = [
                spec for spec in self.registry.tier(tier) if spec.id not in self.explicit_models
            ]
        chain: list[TrackedModel] = []
        for spec in specs:
            if (
                chain
                and (spec.billing == "local" or chain[0].spec.billing == "local")
                and spec.base_url != chain[0].spec.base_url
            ):
                break
            if chain and target:
                status = self._model_status(spec, require_tools=require_tools)
                if not status.selectable:
                    continue
                model = status.model
            else:
                model = self.build_model(spec)
                if model is None and target:
                    break
            if model is not None:
                if require_tools and not _supports_tools(model):
                    raise NoModelAvailable(
                        f"{spec.id} does not support tools. Select a tool-capable model."
                    )
                chain.append(
                    TrackedModel(model, spec, self.ledger, session_id, workspace=workspace)
                )
                if not self.fallback or (target and not fallback_ids):
                    break
        if not chain:
            target = target or f"tier {tier!r}"
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
