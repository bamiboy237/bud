"""An authenticated move destination that cannot run the assistant before commit."""

import os
import shutil
import sqlite3
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated, TextIO

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi import Path as RoutePath
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from bud import __version__
from bud.config import MOVE_DIRECTORY, Settings
from bud.core.auth import OwnerAuthentication
from bud.release import ReleaseError
from bud.release.backup import _atomic_write_bytes
from bud.release.move_archive import MAX_MOVE_BYTES, RuntimeMoveError
from bud.release.runtime_move import verify_runtime

TransferID = Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_receiver(
    settings: Settings,
    token: SecretStr,
    live_lock: TextIO,
    restart: Callable[[], None] | None,
) -> FastAPI:
    if any(
        path.name not in {"auth", "live-server.lock", MOVE_DIRECTORY}
        for path in settings.data_dir.iterdir()
    ):
        raise RuntimeMoveError("Only an empty Bud server can receive a runtime move.")
    root = settings.data_dir / MOVE_DIRECTORY
    root.mkdir(mode=0o700, exist_ok=True)
    sync_directory(settings.data_dir)
    gate = Lock()
    staged = root / "staged"
    active = root / "active"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            live_lock.close()

    app = FastAPI(title="bud runtime receiver", lifespan=lifespan)
    app.add_middleware(OwnerAuthentication, token=token)

    def move_id(folder: Path) -> str | None:
        return (folder / "transfer-id").read_text() if folder.is_dir() else None

    for entry in root.iterdir():
        if entry.name.startswith(("incoming-", "abandoned-")) and entry.is_dir():
            shutil.rmtree(entry)
    prior_stage = move_id(staged)
    if prior_stage is not None and (root / f"cancelled-{prior_stage}").exists():
        shutil.rmtree(staged)
        sync_directory(root)

    def state(transfer: str) -> str:
        current = move_id(active)
        if current is not None:
            return "committed" if current == transfer else "occupied"
        if (root / f"cancelled-{transfer}").exists():
            return "cancelled"
        current = move_id(staged)
        if current is None:
            return "ready"
        return "staged" if current == transfer else "occupied"

    @app.get("/runtime")
    def runtime() -> dict:
        return {
            "version": __version__,
            "mode": "receiving",
            "scope": "runtime-move",
            "needs_onboarding": False,
            "has_model": False,
        }

    @app.get("/runtime-move/{transfer}")
    def status(transfer: TransferID) -> dict:
        with gate:
            return {"state": state(transfer)}

    def publish_stage(transfer: str, archive: Path, candidate: Path) -> dict:
        try:
            verify_runtime(archive, candidate)
        except (ReleaseError, OSError, sqlite3.Error, ValueError, ValidationError, SettingsError):
            raise HTTPException(422, "The archive is not a compatible, idle Bud runtime.") from None
        _atomic_write_bytes(candidate / "transfer-id", transfer.encode())
        _atomic_write_bytes(
            candidate / "data" / "auth" / "owner.token", token.get_secret_value().encode()
        )
        (candidate / "workspace").mkdir(exist_ok=True, mode=0o700)
        for directory, _, _ in os.walk(candidate, topdown=False):
            sync_directory(Path(directory))
        with gate:
            current = state(transfer)
            if current == "ready":
                os.rename(candidate, staged)
                sync_directory(root)
                current = "staged"
            if current not in {"staged", "committed"}:
                raise HTTPException(409, f"This move is {current}.")
            return {"state": current}

    @app.put("/runtime-move/{transfer}")
    async def upload(transfer: TransferID, request: Request) -> dict:
        with gate:
            current = state(transfer)
        if current in {"staged", "committed"}:
            return {"state": current}
        if current != "ready":
            raise HTTPException(409, f"This move is {current}.")
        with TemporaryDirectory(dir=root, prefix="incoming-") as folder:
            archive = Path(folder) / "profile.zip"
            with archive.open("xb") as stream:
                size = 0
                try:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_MOVE_BYTES:
                            raise HTTPException(413, "This Bud profile is too large to move.")
                        stream.write(chunk)
                except ClientDisconnect:
                    raise HTTPException(400, "The upload was interrupted.") from None
            return await run_in_threadpool(
                publish_stage, transfer, archive, Path(folder) / "profile"
            )

    @app.post("/runtime-move/{transfer}/activate")
    def commit(transfer: TransferID, background: BackgroundTasks) -> dict:
        with gate:
            current = state(transfer)
            if current == "staged":
                os.rename(staged, active)
                sync_directory(root)
            elif current != "committed":
                raise HTTPException(409, f"This move is {current}.")
        if restart is not None:
            background.add_task(restart)
        return {"state": "committed"}

    @app.delete("/runtime-move/{transfer}")
    def cancel(transfer: TransferID) -> dict:
        with gate:
            current = state(transfer)
            if current == "committed":
                raise HTTPException(409, "This move is already committed.")
            _atomic_write_bytes(root / f"cancelled-{transfer}", b"")
            sync_directory(root)
            if move_id(staged) == transfer:
                abandoned = root / f"abandoned-{transfer}"
                os.rename(staged, abandoned)
                sync_directory(root)
                shutil.rmtree(abandoned)
        return {"state": "cancelled"}

    return app
