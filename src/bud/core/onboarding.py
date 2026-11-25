"""Safe config.env key management, ChatGPT browser OAuth state, and onboarding snapshots."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTOAuthFlow

from bud.config import Settings, get_settings
from bud.gateway import Gateway
from bud.gateway.chatgpt import (
    LOGIN_TIMEOUT_SECONDS,
    ChatGPTError,
    ChatGPTStore,
    _wait_for_listener,
    refresh_models,
)
from bud.gateway.router import NoModelAvailable
from bud.release.backup import _atomic_write_bytes

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=.*$")


class OnboardingError(Exception):
    """A safe, credential-free onboarding or configuration error."""


@dataclass(frozen=True)
class DefaultModelChoice:
    path: Path

    def read(self) -> str | None:
        if not self.path.is_file():
            return None
        return self.path.read_text(encoding="utf-8").strip() or None

    def write(self, model: str | None) -> None:
        _atomic_write_bytes(self.path, (model or "").encode("utf-8"))


def selectable_models(gateway: Gateway) -> list[str]:
    choices: list[str] = []
    for name in dict.fromkeys(spec.id for spec in gateway.registry.models):
        try:
            gateway.model("frontier", require_tools=True, selected=name)
        except NoModelAvailable:
            continue
        choices.append(name)
    return choices


@dataclass(frozen=True)
class ManagedKeySpec:
    env_name: str
    field_name: str
    group: Literal["model", "integration"]
    label: str
    description: str


MANAGED_KEYS: tuple[ManagedKeySpec, ...] = (
    ManagedKeySpec(
        env_name="OPENAI_API_KEY",
        field_name="openai_api_key",
        group="model",
        label="OpenAI",
        description="GPT models and approval review",
    ),
    ManagedKeySpec(
        env_name="DEEPSEEK_API_KEY",
        field_name="deepseek_api_key",
        group="model",
        label="DeepSeek",
        description="DeepSeek chat and reasoning models",
    ),
    ManagedKeySpec(
        env_name="MISTRAL_API_KEY",
        field_name="mistral_api_key",
        group="model",
        label="Mistral",
        description="Mistral models",
    ),
    ManagedKeySpec(
        env_name="MUSE_API_KEY",
        field_name="muse_api_key",
        group="model",
        label="Meta (Muse)",
        description="Llama models via Muse",
    ),
    ManagedKeySpec(
        env_name="EXA_API_KEY",
        field_name="exa_api_key",
        group="integration",
        label="Exa",
        description="Web search and webpage reading",
    ),
    ManagedKeySpec(
        env_name="COMPOSIO_API_KEY",
        field_name="composio_api_key",
        group="integration",
        label="Composio",
        description="Gmail, Google Calendar, and app integrations",
    ),
)

_KEY_SPECS: dict[str, ManagedKeySpec] = {spec.env_name: spec for spec in MANAGED_KEYS}


def resolve_config_env_path(settings: Settings) -> Path:
    """Resolve the writable env file anchored next to ``settings.data_dir``."""
    base = settings.data_dir.expanduser().resolve().parent
    config_env = base / "config.env"
    if config_env.exists():
        return config_env
    dot_env = base / ".env"
    if dot_env.is_file():
        return dot_env
    return config_env


def _format_env_value(value: str) -> str:
    if any(ch.isspace() or ch in "#\"'$\\" for ch in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'
    return value


def write_env_updates(env_path: Path, updates: Mapping[str, str | None]) -> set[str]:
    """Atomically apply managed key updates to ``env_path`` with 0600 permissions.

    Existing comments, blank lines, and unrelated variables are preserved.
    Passing ``None`` or an empty/whitespace string removes that key.
    """
    if env_path.is_symlink():
        raise OnboardingError("Configuration file must not be a symbolic link.")

    cleaned: dict[str, str | None] = {}
    cleared: set[str] = set()
    for raw_key, raw_value in updates.items():
        if raw_key not in _KEY_SPECS:
            raise OnboardingError(f"Unsupported configuration key: {raw_key}.")
        if raw_value is None:
            cleaned[raw_key] = None
            cleared.add(raw_key)
            continue
        if not isinstance(raw_value, str):
            raise OnboardingError(f"Invalid value for {raw_key}.")
        if any(bad in raw_value for bad in ("\r", "\n", "\x00")):
            raise OnboardingError(f"Value for {raw_key} must be a single line.")
        trimmed = raw_value.strip()
        if not trimmed:
            cleaned[raw_key] = None
            cleared.add(raw_key)
        else:
            cleaned[raw_key] = trimmed

    existing_lines: list[str] = []
    if env_path.exists():
        if not env_path.is_file():
            raise OnboardingError("Configuration path is not a regular file.")
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    handled: set[str] = set()
    out_lines: list[str] = []
    for line in existing_lines:
        match = _ENV_LINE.match(line)
        if match and match.group(1) in cleaned:
            key = match.group(1)
            new_val = cleaned[key]
            if key not in handled:
                handled.add(key)
                if new_val is not None:
                    out_lines.append(f"{key}={_format_env_value(new_val)}")
            continue
        out_lines.append(line)

    for key, new_val in cleaned.items():
        if key not in handled and new_val is not None:
            out_lines.append(f"{key}={_format_env_value(new_val)}")

    payload = "\n".join(out_lines) + ("\n" if out_lines else "")
    env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(dir=env_path.parent, prefix=".config-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, env_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return cleared


def reload_settings(
    current: Settings,
    env_path: Path,
    *,
    cleared_keys: set[str] | None = None,
) -> Settings:
    """Reload managed API keys onto ``current`` while keeping server paths pinned."""
    get_settings.cache_clear()
    cleared = cleared_keys or set()
    parsed = dotenv_values(env_path) if env_path.is_file() else {}
    updates: dict[str, object] = {}
    for spec in MANAGED_KEYS:
        if spec.env_name in cleared:
            updates[spec.field_name] = None
            continue
        raw = parsed.get(spec.env_name)
        if isinstance(raw, str) and raw.strip():
            updates[spec.field_name] = SecretStr(raw.strip())
    return current.model_copy(update=updates) if updates else current


class ChatGPTLoginManager:
    """Coordinate browser-based ChatGPT OAuth for the running server process."""

    def __init__(
        self,
        data_dir: Path,
        *,
        on_before_auth: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
        flow_factory: Callable[..., Any] | None = None,
        wait_listener: Callable[[asyncio.Task[Any]], Awaitable[None]] | None = None,
        refresh_fn: Callable[[ChatGPTStore], Awaitable[list[dict[str, str]]]] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self._on_before_auth = on_before_auth
        self._on_complete = on_complete
        self._flow_factory = flow_factory
        self._wait_listener = wait_listener
        self._refresh_fn = refresh_fn

        self._status: Literal["idle", "waiting", "connected", "error"] = "idle"
        self._url: str | None = None
        self._error: str | None = None
        self._auth_task: asyncio.Task[Any] | None = None
        self._callback_task: asyncio.Task[Any] | None = None
        self._store: ChatGPTStore | None = None
        self._flow: Any | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return current ChatGPT sign-in state without exposing tokens."""
        store = ChatGPTStore(self.data_dir)
        connected = store.connected
        state: dict[str, Any] = {}
        if connected:
            try:
                state = store.state()
            except ChatGPTError:
                connected = False
                state = {}

        waiting = self._auth_task is not None and not self._auth_task.done()
        if waiting:
            status = "waiting"
            url = self._url
            error = None
        elif self._status == "error":
            status = "error"
            url = None
            error = self._error
        else:
            status = "connected" if connected else "idle"
            url = None
            error = None

        raw_models = state.get("models") if connected else []
        models = (
            [
                entry["slug"]
                for entry in raw_models
                if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
            ]
            if isinstance(raw_models, list)
            else []
        )
        selected = state.get("selected_model") if connected else None
        selected_model = selected if isinstance(selected, str) and selected else None
        return {
            "connected": connected,
            "status": status,
            "url": url,
            "error": error,
            "selected_model": selected_model,
            "models": models,
        }

    async def start(self) -> dict[str, Any]:
        """Start ChatGPT OAuth listener and return snapshot containing the HTTPS sign-in URL."""
        if self._auth_task is not None and not self._auth_task.done() and self._url:
            return self.snapshot()

        await self.cancel()
        if self._on_before_auth is not None:
            self._on_before_auth()

        store = ChatGPTStore(self.data_dir)
        store.acquire()
        flow: Any | None = None
        callback_task: asyncio.Task[Any] | None = None
        state = store.state()
        try:
            state.setdefault("host_id", uuid4().urn)
            store.save_state(state)
            previous = await store.load() if store.connected else None
            factory = self._flow_factory or OpenAIChatGPTOAuthFlow
            flow = factory(
                app_name="bud",
                host_id=state["host_id"],
                client_id=previous.client_id if previous else state.get("client_id"),
                id_token_hint=previous.id_token if previous else None,
            )
            callback_task = asyncio.create_task(flow.exchange_code_from_callback())
            wait_fn = self._wait_listener or _wait_for_listener
            await wait_fn(callback_task)
            url = flow.authorization_url(extra_params={"prompt": "consent"})
            if not isinstance(url, str) or not url.startswith("https://"):
                raise OnboardingError("ChatGPT sign-in returned an invalid authorization URL.")
        except Exception as exc:
            if callback_task is not None and not callback_task.done():
                callback_task.cancel()
                await asyncio.gather(callback_task, return_exceptions=True)
            if flow is not None and getattr(flow, "client_id", None) is not None:
                with suppress(Exception):
                    state["client_id"] = flow.client_id
                    store.save_state(state)
            store.close()
            self._status = "error"
            self._url = None
            self._error = str(exc) if isinstance(exc, (ChatGPTError, OnboardingError)) else str(exc)
            raise

        self._store = store
        self._flow = flow
        self._callback_task = callback_task
        self._url = url
        self._error = None
        self._status = "waiting"
        self._auth_task = asyncio.create_task(self._finish_login(store, flow, callback_task))
        return self.snapshot()

    async def _finish_login(
        self,
        store: ChatGPTStore,
        flow: Any,
        callback_task: asyncio.Task[Any],
    ) -> None:
        state = store.state()
        succeeded = False
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
                credentials = await callback_task
            await store.save(credentials)
            state["client_id"] = credentials.client_id
            state.pop("models", None)
            state.pop("selected_model", None)
            store.save_state(state)
            if not getattr(credentials, "plan_usage_granted", True):
                raise ChatGPTError(
                    "Signed in, but ChatGPT plan usage was not granted. "
                    "Sign in again and approve plan usage."
                )
            succeeded = True
        except TimeoutError:
            self._status = "error"
            self._error = "ChatGPT sign-in timed out. Try signing in again."
            self._url = None
        except asyncio.CancelledError:
            self._status = "connected" if store.connected else "idle"
            self._url = None
            raise
        except Exception as exc:
            self._status = "error"
            self._error = (
                str(exc)
                if isinstance(exc, (ChatGPTError, OnboardingError))
                else "ChatGPT sign-in failed. Try again."
            )
            self._url = None
        finally:
            if not callback_task.done():
                callback_task.cancel()
                await asyncio.gather(callback_task, return_exceptions=True)
            if getattr(flow, "client_id", None) is not None:
                with suppress(Exception):
                    state = store.state()
                    state["client_id"] = flow.client_id
                    store.save_state(state)
            store.close()
            self._callback_task = None
            self._store = None
            self._flow = None
            if self._auth_task is asyncio.current_task():
                self._auth_task = None

        if not succeeded:
            return

        try:
            refresh_fn = self._refresh_fn or refresh_models
            await refresh_fn(store)
            if self._on_complete is not None:
                self._on_complete()
            self._status = "connected"
            self._url = None
            self._error = None
        except Exception as exc:
            self._status = "error"
            self._error = (
                str(exc)
                if isinstance(exc, (ChatGPTError, OnboardingError))
                else "Signed in, but loading ChatGPT models failed."
            )
            self._url = None

    async def cancel(self) -> dict[str, Any]:
        """Cancel any in-flight ChatGPT login attempt cleanly."""
        task = self._auth_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._auth_task = None
        if self._callback_task is not None and not self._callback_task.done():
            self._callback_task.cancel()
            await asyncio.gather(self._callback_task, return_exceptions=True)
        self._callback_task = None
        if self._store is not None:
            self._store.close()
            self._store = None
        self._flow = None
        self._url = None
        self._error = None
        self._status = "connected" if ChatGPTStore(self.data_dir).connected else "idle"
        return self.snapshot()

    async def logout(self) -> dict[str, Any]:
        """Disconnect the local ChatGPT session and refresh the live gateway."""
        await self.cancel()
        if self._on_before_auth is not None:
            self._on_before_auth()
        ChatGPTStore(self.data_dir).logout()
        if self._on_complete is not None:
            self._on_complete()
        self._status = "idle"
        self._url = None
        self._error = None
        return self.snapshot()

    async def shutdown(self) -> None:
        """Stop any pending browser login when the server shuts down."""
        await self.cancel()


def onboarding_snapshot(
    settings: Settings,
    gateway: Gateway,
    chatgpt_login: ChatGPTLoginManager,
    *,
    has_model: bool,
    simulate: bool,
    active_model: str | None,
    default_model: str | None,
    model_locked: bool,
) -> dict[str, Any]:
    """Return owner-safe setup status for the Mac onboarding and Settings views."""
    keys: dict[str, dict[str, Any]] = {}
    for spec in MANAGED_KEYS:
        secret = getattr(settings, spec.field_name, None)
        configured = bool(secret is not None and secret.get_secret_value().strip())
        keys[spec.env_name] = {
            "configured": configured,
            "group": spec.group,
            "label": spec.label,
            "description": spec.description,
        }
    return {
        "has_model": has_model,
        "needs_onboarding": not simulate and not has_model,
        "active_model": active_model,
        "default_model": default_model,
        "models": selectable_models(gateway),
        "model_locked": model_locked,
        "config_path": str(resolve_config_env_path(settings)),
        "chatgpt": chatgpt_login.snapshot(),
        "keys": keys,
    }
