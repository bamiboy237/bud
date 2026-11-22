"""Application settings from environment variables and a `.env` file.
The gateway and the core server read API keys and the data directory through `get_settings()`.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

HOST = "127.0.0.1"
PORT = 8420
LOCAL_URL = f"http://{HOST}:{PORT}"


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
    logfire_token: SecretStr | None = Field(default=None, repr=False)

    debug: bool = False
    data_dir: Path = Path("data")
    # Bud's own folder for files it reads and writes. Unset means ~/.bud/workspace.
    workspace_dir: Path | None = None
    # Drive the owner's Chrome through OpenCode Browse when it is installed.
    browser_tools: bool = True
    models_file: Path | None = None
    default_model: str | None = None
    model_fallback: bool = True
    operating_profile: str | None = None
    # Auto mode reviews each write with the OpenAI Decisions API when an OpenAI key exists.
    # Set APPROVAL_REVIEW=false to keep tool calls and requests off that API; auto mode then
    # asks about every write, like strict.
    approval_review: bool = True
    approval_review_model: str = "gpt-6-luna"
    model_api_keys: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    tool_result_retention_hours: float = Field(default=24, gt=0, allow_inf_nan=False)
    # Most bud may spend on model calls in one UTC day, across every conversation and job.
    # Once spent, the next model request is refused until the day rolls over. 0 turns it off.
    daily_spend_usd: Decimal = Field(default=Decimal("5"), ge=0, validation_alias="BUD_DAILY_USD")

    # Spiral detection (bud.harness.spiral). bud has no step cap, so these
    # decide when a run that is going in circles gets a nudge and then a pause.
    loop_detection: bool = True
    # The same tool call with the same result N times in a row. Two in a row is too eager:
    # on normal tasks the model sometimes repeats a read once (a bare `await
    # list_events(...)` that returned nothing) and then corrects itself.
    loop_repeat_note: int = Field(default=3, ge=2)
    # Two different calls alternating (A, B, A, B) for this many cycles.
    loop_cycle_repeats: int = Field(default=3, ge=2)
    # This many consecutive replies without tool calls that say the same thing.
    loop_chant_repeats: int = Field(default=3, ge=2)
    # A model judge asks "is the run making progress?" after this many model requests
    # in a run, then again every `loop_progress_every` requests.
    loop_progress_first: int = Field(default=20, ge=1)
    loop_progress_every: int = Field(default=10, ge=1)
    # Below this probability of progress, the judge counts as a detection.
    loop_progress_threshold: float = Field(default=0.2, ge=0, le=1)
    loop_judge_model: str = "gpt-6-luna"
    loop_judge_timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)

    # A running job with no activity for this long, or open for longer than the deadline,
    # is reported to the owner as stalled.
    job_idle_minutes: float = Field(default=10, gt=0, allow_inf_nan=False)
    job_deadline_minutes: float = Field(default=60, gt=0, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
