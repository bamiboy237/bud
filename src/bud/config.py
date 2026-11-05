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
    composio_api_key: SecretStr | None = None
    exa_api_key: SecretStr | None = None

    debug: bool = False
    data_dir: Path = Path("data")
    tool_result_retention_hours: float = Field(default=24, gt=0, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
