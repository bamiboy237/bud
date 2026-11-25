"""Run a complete move in a disposable Bud image, with no external network."""

import os
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from bud.composio.connection import Connections
from bud.config import Settings
from bud.core.auth import owner_token
from bud.core.log import EventLog
from bud.release.runtime_move_client import move_runtime


@contextmanager
def running(home: Path, port: int):
    with (
        (home / "server.log").open("wb") as log,
        subprocess.Popen(
            ["bud", "serve", "--home", str(home), "--host", "127.0.0.1", "--port", str(port)],
            env={**os.environ, "BUD_RECEIVE_MOVE": "true", "BROWSER_TOOLS": "false"},
            stdout=log,
            stderr=log,
        ) as server,
    ):
        try:
            yield server
        finally:
            server.terminate()
            server.wait(timeout=15)


def status(client: httpx.Client, server: subprocess.Popen) -> dict:
    for _ in range(80):
        try:
            return client.get("/runtime").raise_for_status().json()
        except httpx.TransportError:
            if server.poll() is not None:
                raise AssertionError("Bud exited before it started.") from None
            time.sleep(0.15)
    raise AssertionError("Bud did not start.")


class LostCommitResponse(Exception):
    pass


class DropCommitResponse(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if request.method == "POST" and request.url.path.endswith("/activate"):
            response.read()
            response.raise_for_status()
            response.close()
            raise LostCommitResponse
        return response


def interrupt_move(source: Path, url: str, token: str) -> None:
    new_client = httpx.Client
    with patch.object(
        httpx, "Client", side_effect=lambda **kw: new_client(**kw, transport=DropCommitResponse())
    ):
        try:
            move_runtime(source, url, token)
        except LostCommitResponse:
            return
    raise AssertionError("The move did not reach the commit checkpoint.")


def smoke() -> None:
    assert os.getuid() != 0
    subprocess.run(["browse", "version"], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        store = Connections(Settings(data_dir=source / "data"))
        scope = store.store.session_scope(store.stored())
        chats = EventLog(store.folder / "sessions" / scope / "bud.db")
        chat = chats.primary_session()
        chats.append(chat, "user.message", {"text": "Portable conversation"})
        (source / "workspace").mkdir()
        (source / "workspace" / "report.txt").write_text("Portable workspace")
        target = root / "target"
        token = owner_token(Settings(data_dir=target / "data")).get_secret_value()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=3, trust_env=False
        ) as client:
            for first in (True, False):
                with running(target, port) as server:
                    runtime = status(client, server)
                    if first:
                        assert runtime["mode"] == "receiving"
                        assert client.get(f"/sessions/{chat}").status_code == 404
                        interrupt_move(source, url, token)
                        for _ in range(80):
                            runtime = status(client, server)
                            if runtime["mode"] == "accounts":
                                break
                            time.sleep(0.15)
                    else:
                        for action in (["--cancel"], []):
                            subprocess.run(
                                ["bud", "move-runtime", str(source), url, *action],
                                input=token + "\n",
                                text=True,
                                check=True,
                                timeout=65,
                            )
                        frozen = subprocess.run(
                            ["bud", "serve", "--home", str(source), "--port", str(port)],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        assert frozen.returncode != 0 and "stopped for a move" in frozen.stderr
                    assert runtime["mode"] == "accounts" and runtime["received_move_id"]
                    assert client.get(f"/sessions/{chat}").status_code == 200
                    assert client.get("/sessions").json()["conversations"][0]["title"] == (
                        "Portable conversation"
                    )
    print("The Linux image moved a profile, kept the source stopped, and reopened it after reboot.")


if __name__ == "__main__":
    smoke()
