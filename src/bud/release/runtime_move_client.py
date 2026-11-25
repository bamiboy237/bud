"""Stop the source and drive one recoverable runtime move to a remote Bud."""

import json
import plistlib
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from bud import __version__
from bud.config import MOVE_FENCE, Settings
from bud.core.runtime_receiver import sync_directory
from bud.core.server import hold_single_server_lock
from bud.release.backup import _atomic_write_bytes
from bud.release.move_archive import RuntimeMoveError, source_paths
from bud.release.runtime_move import pack_runtime
from bud.release.service import ServiceManager


def _origin(address: str) -> httpx.URL:
    origin = httpx.URL(address)
    loopback = origin.host in {"127.0.0.1", "localhost", "::1"}
    if (
        not origin.host
        or (origin.scheme != "https" and not (origin.scheme == "http" and loopback))
        or origin.userinfo
        or origin.path != "/"
        or origin.query
        or origin.fragment
    ):
        raise RuntimeMoveError("Use the Bud server’s HTTPS address without a path.")
    return origin


def _installed_service(home: Path) -> ServiceManager | None:
    if sys.platform != "darwin":
        return None
    service = ServiceManager(home)
    if not service.plist_path.is_file():
        return None
    config = plistlib.loads(service.plist_path.read_bytes())
    return service if config.get("WorkingDirectory") == str(home) else None


def _active(client: httpx.Client, transfer: str) -> bool:
    response = client.get("/runtime", timeout=3)
    if response.status_code in {502, 503, 504}:
        return False
    runtime = response.raise_for_status().json()
    return runtime.get("received_move_id") == transfer and runtime.get("mode") == "accounts"


def _wait_for_runtime(client: httpx.Client, transfer: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if _active(client, transfer):
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeMoveError("The server has not started Bud yet. Check the same move again.")


def move_runtime(
    home: Path, address: str, token: str, *, workspace: Path | None = None, cancel: bool = False
) -> dict:
    home = home.expanduser().resolve()
    service = _installed_service(home)
    data, workspace = (
        (service.data_dir, workspace or home / "workspace")
        if service is not None
        else source_paths(home, workspace)
    )
    if not data.is_dir():
        raise RuntimeMoveError("Choose the folder for the Bud runtime on this Mac.")
    data = data.resolve()
    fence = data / MOVE_FENCE
    origin = _origin(address)
    config = next((home / name for name in ("config.env", ".env") if (home / name).is_file()), None)
    with (
        hold_single_server_lock(data / "runtime-move.lock"),
        httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(120, connect=6),
            trust_env=False,
        ) as client,
    ):
        try:
            runtime = client.get("/runtime").raise_for_status().json()
            if fence.exists():
                record = json.loads(fence.read_bytes())
                if record["destination"] != str(origin):
                    raise RuntimeMoveError("Open the same destination to finish this Bud move.")
                transfer = record["id"]
                workspace = Path(record["workspace"])
            else:
                if cancel:
                    return {"state": "cancelled"}
                if runtime.get("mode") != "receiving" or runtime.get("version") != __version__:
                    raise RuntimeMoveError(
                        "Choose an empty server set up to receive this version of Bud."
                    )
                transfer = uuid4().hex
                record = {"id": transfer, "destination": str(origin), "workspace": str(workspace)}
                _atomic_write_bytes(fence, json.dumps(record).encode())
                sync_directory(data)

            route = f"/runtime-move/{transfer}"
            if runtime.get("received_move_id") == transfer:
                return {"state": "complete"}
            if runtime.get("mode") != "receiving":
                raise RuntimeMoveError(
                    "This server cannot confirm the move. Bud stays stopped here."
                )
            if cancel:
                result = client.delete(route).raise_for_status().json()
                if result.get("state") != "cancelled":
                    raise RuntimeMoveError("The server did not confirm cancellation.")
                fence.unlink()
                sync_directory(data)
                if service is not None:
                    service.start()
                return {"state": "cancelled"}

            state = client.get(route).raise_for_status().json().get("state")
            if state == "ready":
                if service is not None:
                    service.stop()
                settings = Settings(_env_file=config).model_copy(
                    update={"data_dir": data, "workspace_dir": workspace}
                )
                with (
                    hold_single_server_lock(data / "live-server.lock"),
                    TemporaryDirectory(prefix="bud-runtime-") as folder,
                    ExitStack() as locks,
                ):
                    chatgpt = data / "chatgpt"
                    if chatgpt.is_dir() and not chatgpt.is_symlink():
                        locks.enter_context(hold_single_server_lock(chatgpt / "credentials.lock"))
                    archive = Path(folder) / "profile.zip"
                    pack_runtime(settings, archive)
                    with archive.open("rb") as stream:
                        client.put(
                            route,
                            content=stream,
                            headers={
                                "Content-Type": "application/zip",
                                "Content-Length": str(archive.stat().st_size),
                            },
                        ).raise_for_status()
                state = "staged"
            if state in {"staged", "committed"}:
                with suppress(httpx.TransportError):
                    client.post(f"{route}/activate").raise_for_status()
                _wait_for_runtime(client, transfer)
                return {"state": "complete"}
            raise RuntimeMoveError("This move is not ready. Check the destination and try again.")
        except httpx.HTTPStatusError as exc:
            messages = {
                401: "The server didn’t accept its access token.",
                409: "The server rejected this step. Check the same move again.",
                422: "The server could not verify this Bud runtime.",
            }
            raise RuntimeMoveError(
                messages.get(exc.response.status_code, "The server could not finish the move.")
            ) from None
        except httpx.TransportError:
            raise RuntimeMoveError(
                "Couldn’t reach the server. Check the same move again."
            ) from None
