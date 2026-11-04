"""Local ChatGPT sign-in state; Pydantic AI owns OAuth and token refresh."""

import asyncio
import fcntl
import json
import os
import tempfile
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from pydantic_ai.providers.openai_chatgpt import (
    OpenAIChatGPTCredentials,
    OpenAIChatGPTCredentialSource,
    OpenAIChatGPTOAuthFlow,
    OpenAIChatGPTProvider,
)

LOGIN_TIMEOUT_SECONDS = 300
CALLBACK_PORT = 1455


class ChatGPTError(Exception):
    """A safe, user-facing sign-in or local-storage error."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise ChatGPTError("ChatGPT state must not be a symbolic link.")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise ChatGPTError(f"Cannot read ChatGPT state in {path.parent}.") from None


def _save_json(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".chatgpt-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ChatGPTStore(OpenAIChatGPTCredentialSource):
    def __init__(self, data_dir: Path) -> None:
        self.folder = data_dir / "chatgpt"
        self.path = self.folder / "credentials.json"
        self.state_path = self.folder / "installation.json"
        self._lock: TextIO | None = None

    @property
    def connected(self) -> bool:
        return self.path.is_file()

    def state(self) -> dict[str, Any]:
        return _read_json(self.state_path)

    def acquire(self) -> None:
        if self._lock is not None:
            return
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.folder.chmod(0o700)
        lock = (self.folder / "credentials.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ChatGPTError(
                "Another bud process is using ChatGPT. Stop it before signing in, "
                "listing ChatGPT models, or starting another ChatGPT process."
            ) from None
        self._lock = lock

    def close(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    async def load(self) -> OpenAIChatGPTCredentials:
        self.acquire()
        data = _read_json(self.path)
        try:
            credentials = OpenAIChatGPTCredentials(**data)
            if not all(
                isinstance(value, str) and value
                for value in (
                    credentials.client_id,
                    credentials.access_token,
                    credentials.refresh_token,
                )
            ) or not isinstance(credentials.scope, str):
                raise ValueError
        except (TypeError, ValueError):
            raise ChatGPTError(
                "ChatGPT credentials are missing or invalid. Run `bud login chatgpt`."
            ) from None
        self.path.chmod(0o600)
        return credentials

    async def save(self, credentials: OpenAIChatGPTCredentials) -> None:
        self.acquire()
        _save_json(self.path, asdict(credentials))

    def save_state(self, state: dict[str, Any]) -> None:
        self.acquire()
        _save_json(self.state_path, state)

    def logout(self) -> None:
        self.acquire()
        try:
            self.path.unlink(missing_ok=True)
            state = self.state()
            state.pop("models", None)
            state.pop("selected_model", None)
            self.save_state(state)
        finally:
            self.close()


async def refresh_models(store: ChatGPTStore) -> list[dict[str, str]]:
    """Use the provider's authenticated client so catalog reads also refresh stale tokens."""
    store.acquire()
    provider = OpenAIChatGPTProvider(credential_source=store)
    try:
        async with provider:
            catalog = await provider.client.get("/models", cast_to=dict[str, Any])
        models = [
            {"slug": model["slug"], "display_name": model.get("display_name", model["slug"])}
            for model in catalog.get("models", [])
            if model.get("visibility") == "list" and isinstance(model.get("slug"), str)
        ]
        if not models:
            raise ChatGPTError("ChatGPT returned no selectable models for this account.")
        state = store.state()
        state["models"] = models
        if state.get("selected_model") not in {model["slug"] for model in models}:
            state["selected_model"] = models[0]["slug"]
        store.save_state(state)
        return models
    finally:
        store.close()


async def _wait_for_listener(task: asyncio.Task) -> None:
    for _ in range(50):
        if task.done():
            await task
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", CALLBACK_PORT)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        try:
            writer.write(b"GET /ready HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
        if task.done():
            await task
        return
    raise ChatGPTError(f"ChatGPT callback listener did not start on port {CALLBACK_PORT}.")


async def login(data_dir: Path, *, no_browser: bool = False) -> None:
    store = ChatGPTStore(data_dir)
    store.acquire()
    task: asyncio.Task | None = None
    flow: OpenAIChatGPTOAuthFlow | None = None
    state = store.state()
    try:
        state.setdefault("host_id", uuid4().urn)
        store.save_state(state)
        previous = await store.load() if store.connected else None
        flow = OpenAIChatGPTOAuthFlow(
            app_name="bud",
            host_id=state["host_id"],
            client_id=previous.client_id if previous else state.get("client_id"),
            id_token_hint=previous.id_token if previous and not no_browser else None,
        )
        async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
            task = asyncio.create_task(flow.exchange_code_from_callback())
            await _wait_for_listener(task)
            url = flow.authorization_url(extra_params={"prompt": "consent"})
            if no_browser:
                print(url, flush=True)
            elif not webbrowser.open(url):
                raise ChatGPTError(
                    "Could not open the browser. Run `bud login chatgpt --no-browser`."
                )
            print(
                "Finish ChatGPT sign-in in your browser. Waiting up to five minutes...", flush=True
            )
            credentials = await task
            await store.save(credentials)
            state["client_id"] = credentials.client_id
            state.pop("models", None)
            state.pop("selected_model", None)
            store.save_state(state)
            if not credentials.plan_usage_granted:
                raise ChatGPTError(
                    "Signed in, but ChatGPT plan usage was not granted. "
                    "Run `bud login chatgpt` and approve plan usage."
                )
    except TimeoutError:
        raise ChatGPTError(
            "ChatGPT sign-in timed out. Run `bud login chatgpt` to try again."
        ) from None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if flow is not None and flow.client_id is not None:
            state["client_id"] = flow.client_id
            store.save_state(state)
        store.close()
    models = await refresh_models(store)
    print(f"ChatGPT connected. {len(models)} models available.")
    print(f"Default model: openai-chatgpt:{store.state()['selected_model']}")
