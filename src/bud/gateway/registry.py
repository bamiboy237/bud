"""Loads the model registry from `models.toml` and prices requests.
A `ModelSpec` holds one model's provider, tier, and per-million-token prices, and `cost`
turns a request's usage into USD.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai.usage import RequestUsage

Tier = Literal["frontier", "fast"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    name: str
    tier: Tier
    input: float
    output: float
    cached: float
    cache_write: float | None = None

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.name}"

    def cost(self, usage: RequestUsage) -> float:
        """Providers report cached tokens inside input_tokens."""
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
