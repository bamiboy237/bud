"""Loads the model registry from `models.toml` and prices requests.
A `ModelSpec` holds one model's provider, tier, and per-million-token prices, and `cost`
turns a request's usage into USD.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.usage import RequestUsage

Tier = Literal["frontier", "fast"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    name: str
    tier: Tier
    billing: Literal["api", "subscription"] = "api"
    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cached: float = Field(default=0, ge=0)
    cache_write: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_api_prices(self) -> Self:
        if self.billing == "api" and not {"input", "output", "cached"} <= self.model_fields_set:
            raise ValueError("API models require input, output, and cached token prices.")
        return self

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.name}"

    def cost(self, usage: RequestUsage) -> float | None:
        """Subscription tokens have no per-request API dollar price."""
        if self.billing == "subscription":
            return None
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
    models: list[ModelSpec]

    def tier(self, tier: Tier) -> list[ModelSpec]:
        return [spec for spec in self.models if spec.tier == tier]


def load_registry(path: Path | None = None) -> Registry:
    if path is None:
        text = resources.files("bud.gateway").joinpath("models.toml").read_text()
    else:
        text = path.read_text()
    return Registry.model_validate(tomllib.loads(text))
