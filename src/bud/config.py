"""Application settings from environment variables and a `.env` file.
The gateway and the core server read API keys and the data directory through `get_settings()`.
"""

import os
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

HOST = "127.0.0.1"
PORT = 8420
LOCAL_URL = f"http://{HOST}:{PORT}"
MOVE_DIRECTORY = ".runtime-move"
MOVE_FENCE = ".runtime-move-source.json"


def default_bud_home() -> Path:
    """Default home folder for an installed daily-driver Bud (`~/.bud`)."""
    return Path.home() / ".bud"


def resolve_runtime_paths(
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, Path, Path | None]:
    """Resolve ``(env_file, default_data_dir, default_workspace_dir)`` for the current process.

    Precedence preserves developer checkouts while enabling standalone `~/.bud` installs:
    1. Explicit `BUD_HOME` env var -> `<BUD_HOME>/config.env` (or `.env`), `<BUD_HOME>/data`, and
       `<BUD_HOME>/workspace`.
    2. Current working directory containing `.env` or `data/` -> legacy repo-checkout defaults.
    3. Installed `~/.bud` containing `config.env` or `data/` -> standalone home defaults.
    4. Fallback -> `.env`, `Path("data")`, `None`.
    """
    current = (cwd or Path.cwd()).expanduser()
    environ = env if env is not None else os.environ
    bud_home = environ.get("BUD_HOME", "").strip()
    if bud_home:
        home = Path(bud_home).expanduser()
        env_file: Path | None = None
        if (home / "config.env").is_file():
            env_file = home / "config.env"
        elif (home / ".env").is_file():
            env_file = home / ".env"
        return env_file, home / "data", home / "workspace"
    if (current / ".env").is_file() or (current / "data").is_dir():
        return current / ".env", Path("data"), None
    home = default_bud_home()
    if (home / "config.env").is_file() or (home / "data").is_dir():
        env_file = home / "config.env" if (home / "config.env").is_file() else None
        return env_file, home / "data", home / "workspace"
    return Path(".env"), Path("data"), None


class Settings(BaseSettings):
    deepseek_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    muse_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    composio_api_key: SecretStr | None = None
    exa_api_key: SecretStr | None = None
    owner_token: SecretStr | None = Field(default=None, repr=False)
    receive_move: bool = Field(default=False, validation_alias="BUD_RECEIVE_MOVE")
    received_move_id: str | None = None
    photon_api_key: SecretStr | None = Field(default=None, repr=False)
    photon_project_id: str | None = Field(default=None, repr=False)
    photon_owner_handle: str | None = Field(default=None, repr=False)
    photon_agent_handle: str | None = Field(default=None, repr=False)
    logfire_token: SecretStr | None = Field(default=None, repr=False)

    debug: bool = False
    data_dir: Path = Path("data")
    # Bud's own folder for files it reads and writes. Unset means ~/.bud/workspace.
    workspace_dir: Path | None = None
    # Server bind host and port, and isolated Chrome relay overrides.
    host: str = Field(default=HOST, validation_alias="BUD_HOST")
    port: int = Field(default=PORT, ge=1, le=65535, validation_alias="BUD_PORT")
    browse_home: Path | None = Field(default=None, validation_alias="BUD_BROWSE_HOME")
    browse_port: int = Field(default=19989, ge=1, le=65535, validation_alias="BUD_BROWSE_PORT")
    browse_peer_ip: str | None = Field(default=None, validation_alias="BUD_BROWSE_PEER_IP")
    # Drive the owner's Chrome through OpenCode Browse when it is installed.
    browser_tools: bool = True
    models_file: Path | None = None
    default_model: str | None = None
    model_fallback: bool = True
    operating_profile: str | None = None
    # Auto mode reviews each write with the OpenAI Decisions API when an OpenAI key exists.
    # With APPROVAL_REVIEW=false, Bud asks about calls that would have gone to the reviewer.
    # Auto mode still allows workspace writes. Use strict mode to ask about those too.
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
    env_file, default_data_dir, default_workspace_dir = resolve_runtime_paths()
    settings = Settings(_env_file=env_file) if env_file is not None else Settings(_env_file=None)
    updates: dict[str, object] = {}
    bud_home = os.environ.get("BUD_HOME", "").strip()
    home_root = Path(bud_home).expanduser() if bud_home else default_data_dir.parent
    if "data_dir" not in settings.model_fields_set and default_data_dir != settings.data_dir:
        updates["data_dir"] = default_data_dir
    elif (
        default_data_dir.is_absolute()
        and not settings.data_dir.is_absolute()
        and not os.environ.get("DATA_DIR", "").strip()
    ):
        updates["data_dir"] = home_root / settings.data_dir
    if "workspace_dir" not in settings.model_fields_set and default_workspace_dir is not None:
        updates["workspace_dir"] = default_workspace_dir
    settings = settings.model_copy(update=updates)
    active = settings.data_dir / MOVE_DIRECTORY / "active"
    if settings.receive_move and active.is_dir():
        return Settings(_env_file=active / "config.env").model_copy(
            update={
                "data_dir": active / "data",
                "workspace_dir": active / "workspace",
                "received_move_id": (active / "transfer-id").read_text().strip(),
            }
        )
    return settings
