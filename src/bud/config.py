"""Application settings from environment variables and a `.env` file.
The gateway and the core server read API keys and the data directory through `get_settings()`.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    deepseek_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    muse_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    composio_api_key: SecretStr | None = None
    exa_api_key: SecretStr | None = None
    owner_token: SecretStr | None = Field(default=None, repr=False)
    photon_api_key: SecretStr | None = Field(default=None, repr=False)
    photon_project_id: str | None = Field(default=None, repr=False)
    photon_owner_handle: str | None = Field(default=None, repr=False)
    photon_agent_handle: str | None = Field(default=None, repr=False)

    debug: bool = False
    data_dir: Path = Path("data")
    models_file: Path | None = None
    default_model: str | None = None
    model_fallback: bool = True
    # Opt-in: in balanced mode, a decision model scores eligible writes and a score at or above
    # the threshold skips the owner's approval. Off by default; strict never uses it.
    decision_auto_allow: bool = False
    decision_threshold: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)
    model_api_keys: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    tool_result_retention_hours: float = Field(default=24, gt=0, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
