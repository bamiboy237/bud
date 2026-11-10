"""Wires the model registry, credential sources, and request ledger."""

import re

from bud.config import Settings, get_settings
from bud.gateway.chatgpt import ChatGPTStore
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelConfigurationError, ModelSpec, Registry, load_registry
from bud.gateway.router import Gateway, NoModelAvailable, provider_model_factory

__all__ = ["Gateway", "Ledger", "NoModelAvailable", "build_gateway"]


def build_gateway(
    settings: Settings | None = None,
    *,
    providers: set[str] | None = None,
    model_id: str | None = None,
) -> Gateway:
    settings = settings or get_settings()
    registry = load_registry(settings.models_file)
    model_id = model_id or settings.default_model
    if model_id is not None and not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,63}:[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,255}", model_id
    ):
        raise ModelConfigurationError("Select a provider:model ID from `bud models`.")
    chatgpt = ChatGPTStore(settings.data_dir)
    use_chatgpt = (model_id is None and settings.models_file is None) or (
        model_id is not None and model_id.startswith("openai-chatgpt:")
    )
    state = chatgpt.state() if chatgpt.connected and use_chatgpt else {}
    selected = state.get("selected_model")
    chatgpt_models = [
        ModelSpec(provider="openai-chatgpt", name=model["slug"], tier=tier, billing="subscription")
        for model in state.get("models", [])
        for tier in ("fast", "frontier")
    ]
    if model_id is not None:
        registry = Registry(
            models=[spec for spec in chatgpt_models + registry.models if spec.id == model_id]
        )
        if not registry.models:
            raise NoModelAvailable(
                f"Unknown model: {model_id}. Run `bud models` for available IDs."
            )
    elif chatgpt.connected and settings.models_file is None:
        if not selected or selected not in {spec.name for spec in chatgpt_models}:
            raise NoModelAvailable("ChatGPT has no saved model catalog. Run `bud models`.")
        seen: set[tuple[str, str]] = set()
        registry = Registry(
            models=[
                spec
                for spec in [spec for spec in chatgpt_models if spec.name == selected]
                + registry.models
                if (spec.id, spec.tier) not in seen and not seen.add((spec.id, spec.tier))
            ]
        )
    if providers is not None:
        registry = Registry(
            models=[
                spec
                for spec in registry.models
                if spec.provider in providers or spec.allow_accounts
            ]
        )
    return Gateway(
        registry,
        Ledger(settings.data_dir / "bud.db"),
        provider_model_factory(settings, chatgpt),
        selected_model=model_id,
        chatgpt=chatgpt,
        fallback=settings.model_fallback,
    )
