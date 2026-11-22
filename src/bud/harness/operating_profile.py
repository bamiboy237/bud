"""Versioned, immutable model operating profiles with deterministic content digests.

An operating profile pins execution behavior across five areas: model selection/settings,
harness composition, context compaction budgets, tool-result handling, and execution/retry
limits. Permission and side-effect gates (`bud.harness.actions`, `bud.core.profiles`) live
outside operating profiles and remain identical across every profile.
"""

from __future__ import annotations

import json
import os
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bud.gateway.registry import Tier

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
EndStrategy = Literal["early", "graceful", "exhaustive"]


class ModelProfileConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    tier: Tier = "fast"
    model_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}:[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,255}$",
    )
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: ReasoningEffort | None = None


class HarnessCompositionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    instruction_style: str = Field(default="default", min_length=1, max_length=64)
    instruction_addendum: str = ""
    defer_capabilities: bool = True
    code_mode_enabled: bool = True
    code_mode_max_retries: int = Field(default=2, ge=0)
    code_mode_max_tool_calls: int = Field(default=12, ge=1)
    code_mode_max_duration_secs: float = Field(default=10.0, gt=0)
    code_mode_max_memory_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    code_mode_max_suspensions: int = Field(default=2_000, gt=0)
    long_run_reminder_every: int | None = Field(default=8, ge=1)
    caching_enabled: bool = True
    repair_tool_arguments: bool = True
    warn_on_cache_busts: bool = True


class ContextBudgetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_fraction: float = Field(default=0.65, gt=0, lt=1)
    background_fraction: float = Field(default=0.8, gt=0, lt=1)
    fallback_context_window: int = Field(default=64_000, gt=0)
    keep_tokens: int = Field(default=12_000, ge=0)
    compacted_target: float = Field(default=0.7, gt=0, lt=1)
    clear_keep_pairs: int = Field(default=4, ge=0)
    clear_min_tokens: int = Field(default=2_000, ge=0)
    summary_max_tokens: int = Field(default=4_096, gt=0)
    summary_tool_return_max_chars: int = Field(default=4_000, gt=0)
    max_age_seconds: float = Field(default=300.0, gt=0)
    max_preparations: int = Field(default=64, ge=1)


class ToolResultConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    spill_chars: int = Field(default=10_000, gt=0)
    preview_chars: int = Field(default=1_000, ge=0)
    truncate_max_chars: int = Field(default=4_000, gt=0)
    history_spill_chars: int = Field(default=10_000, gt=0)
    retention_hours: float = Field(default=24.0, gt=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> ToolResultConfig:
        if self.preview_chars > self.spill_chars:
            raise ValueError("preview_chars must not exceed spill_chars")
        if self.truncate_max_chars > self.spill_chars:
            raise ValueError("truncate_max_chars must not exceed spill_chars")
        return self


class ExecutionLimitsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    request_limit: int | None = Field(default=None, ge=1)
    tool_calls_limit: int | None = Field(default=None, ge=1)
    total_tokens_limit: int | None = Field(default=None, ge=1)
    retries: int = Field(default=1, ge=0)
    output_retries: int | None = Field(default=None, ge=0)
    tool_timeout: float | None = Field(default=None, gt=0)
    end_strategy: EndStrategy = "graceful"
    spiral_enabled: bool | None = None
    spiral_repeat_note: int = Field(default=3, ge=2)
    spiral_cycle_repeats: int = Field(default=3, ge=2)
    spiral_chant_repeats: int = Field(default=3, ge=2)
    spiral_progress_first: int = Field(default=20, ge=1)
    spiral_progress_every: int = Field(default=10, ge=1)
    spiral_progress_threshold: float = Field(default=0.2, ge=0, le=1)
    spiral_judge_timeout: float = Field(default=15.0, gt=0)


class OperatingProfile(BaseModel):
    """Immutable, versioned operating profile with a canonical SHA-256 content digest."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=64)
    previous_version: str | None = None
    model: ModelProfileConfig = Field(default_factory=ModelProfileConfig)
    harness: HarnessCompositionConfig = Field(default_factory=HarnessCompositionConfig)
    context: ContextBudgetConfig = Field(default_factory=ContextBudgetConfig)
    tool_results: ToolResultConfig = Field(default_factory=ToolResultConfig)
    limits: ExecutionLimitsConfig = Field(default_factory=ExecutionLimitsConfig)

    @cached_property
    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode()).hexdigest()

    def attribution(self) -> dict[str, str]:
        return {
            "id": self.id,
            "version": self.version,
            "digest": self.digest,
        }

    def derive(
        self,
        *,
        version: str,
        id: str | None = None,
        model: ModelProfileConfig | None = None,
        harness: HarnessCompositionConfig | None = None,
        context: ContextBudgetConfig | None = None,
        tool_results: ToolResultConfig | None = None,
        limits: ExecutionLimitsConfig | None = None,
    ) -> OperatingProfile:
        """Create a new versioned profile from this profile without mutating it."""
        target_id = id or self.id
        return OperatingProfile(
            id=target_id,
            version=version,
            previous_version=self.version if target_id == self.id else None,
            model=model if model is not None else self.model,
            harness=harness if harness is not None else self.harness,
            context=context if context is not None else self.context,
            tool_results=tool_results if tool_results is not None else self.tool_results,
            limits=limits if limits is not None else self.limits,
        )


BASELINE_PROFILE = OperatingProfile(id="baseline", version="1")


class OperatingProfileStore:
    """Persists the active operating profile and its rollback target in `data_dir`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> OperatingProfile:
        state = self._read_state()
        raw = state.get("active")
        if raw is None:
            return BASELINE_PROFILE
        return OperatingProfile.model_validate(raw)

    def previous(self) -> OperatingProfile | None:
        state = self._read_state()
        raw = state.get("previous")
        return OperatingProfile.model_validate(raw) if raw is not None else None

    def activate(self, profile: OperatingProfile) -> OperatingProfile:
        current = self.load()
        previous = current if current.digest != profile.digest else self.previous()
        self._write_state(profile, previous)
        return profile

    def rollback(self) -> OperatingProfile:
        target = self.previous() or BASELINE_PROFILE
        current = self.load()
        if current.digest == target.digest:
            return target
        self._write_state(target, None)
        return target

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, active: OperatingProfile, previous: OperatingProfile | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {
            "active": active.model_dump(mode="json"),
            "previous": previous.model_dump(mode="json") if previous is not None else None,
        }
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)


def resolve_operating_profile(
    selector: str | None = None,
    *,
    store: OperatingProfileStore | None = None,
) -> OperatingProfile:
    """Resolve an operating profile from an explicit selector, store, or baseline."""
    if selector is None or selector == "baseline" or selector == "baseline@1":
        if selector is None and store is not None:
            return store.load()
        return BASELINE_PROFILE
    candidate = Path(selector)
    if candidate.is_file():
        return OperatingProfile.model_validate_json(candidate.read_text())
    if store is not None:
        active = store.load()
        if selector in {active.id, f"{active.id}@{active.version}", active.digest}:
            return active
        prev = store.previous()
        if prev is not None and selector in {prev.id, f"{prev.id}@{prev.version}", prev.digest}:
            return prev
    raise ValueError(f"Unknown operating profile: {selector!r}")
