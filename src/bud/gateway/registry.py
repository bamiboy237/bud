"""Loads the model registry from `models.toml` and prices requests.
A `ModelSpec` holds one model's provider, tier, and per-million-token prices, and `cost`
turns a request's usage into USD.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_ai.usage import RequestUsage

Tier = Literal["frontier", "fast"]


class ModelConfigurationError(Exception):
    pass


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+-]*$")
    tier: Tier
    billing: Literal["api", "subscription", "local"] = "api"
    base_url: str | None = None
    protocol: Literal["chat", "responses"] | None = None
    supports_tools: bool | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = (
        None
    )
    allow_accounts: bool = False
    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cached: float = Field(default=0, ge=0)
    cache_write: float | None = Field(default=None, ge=0)

    @field_validator("base_url")
    @classmethod
    def endpoint_without_credentials(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
            ):
                raise ValueError("Use an HTTP(S) endpoint without credentials, query or fragment.")
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Non-loopback model endpoints require HTTPS.")
            _ = url.port
        return value

    @model_validator(mode="after")
    def require_api_prices(self) -> Self:
        if self.billing == "api" and not {"input", "output", "cached"} <= self.model_fields_set:
            raise ValueError("API models require input, output, and cached token prices.")
        if self.provider == "openai-chatgpt":
            if self.base_url or self.billing != "subscription" or self.protocol == "chat":
                raise ValueError("ChatGPT sign-in uses its native subscription Responses route.")
        elif self.billing == "subscription":
            raise ValueError("Subscription billing requires ChatGPT sign-in.")
        if self.billing == "local" and not self.base_url:
            raise ValueError("Local billing requires an explicit endpoint.")
        if self.base_url and (self.protocol is None or self.supports_tools is None):
            raise ValueError("Custom endpoints require protocol and supports_tools.")
        if not self.base_url and self.provider not in {
            "openai-chatgpt",
            "openai",
            "deepseek",
            "meta",
            "mistral",
        }:
            raise ValueError("Custom providers require an endpoint.")
        if self.base_url and self.provider in {"openai", "deepseek", "meta"}:
            raise ValueError("Give a custom endpoint its own provider name.")
        return self

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.name}"

    def cost(self, usage: RequestUsage) -> float | None:
        """Subscription tokens have no per-request API dollar price."""
        if self.billing == "subscription":
            return None
        if self.billing == "local":
            return 0.0
        cache_write_price = self.input if self.cache_write is None else self.cache_write
        uncached = usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens
        total = (
            uncached * self.input
            + usage.cache_read_tokens * self.cached
            + usage.cache_write_tokens * cache_write_price
            + usage.output_tokens * self.output
        )
        return total / 1_000_000


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ModelSpec]

    @model_validator(mode="after")
    def unique_routes(self) -> Self:
        routes = {}
        tiers = set()
        for spec in self.models:
            key = (spec.id, spec.tier)
            if key in tiers:
                raise ValueError("Duplicate model in a tier.")
            tiers.add(key)
            definition = spec.model_dump(exclude={"tier"})
            if spec.id in routes and routes[spec.id] != definition:
                raise ValueError("A model must have the same definition across tiers.")
            routes[spec.id] = definition
        return self

    def tier(self, tier: Tier) -> list[ModelSpec]:
        return [spec for spec in self.models if spec.tier == tier]


def load_registry(path: Path | None = None) -> Registry:
    try:
        text = (
            resources.files("bud.gateway").joinpath("models.toml").read_text()
            if path is None
            else path.read_text()
        )
        return Registry.model_validate(tomllib.loads(text))
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        raise ModelConfigurationError(f"Invalid model configuration at: {fields}.") from None
    except (ValueError, OSError):
        raise ModelConfigurationError(
            "Cannot read model registry; check MODELS_FILE and TOML."
        ) from None
