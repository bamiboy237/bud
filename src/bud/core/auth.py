"""Single-owner credentials and an early HTTP authentication boundary.

The credential belongs to DATA_DIR, not a connected app/account or session. Changing
account scope must not change who may access the server. This is not transport
encryption or permission to expose the localhost server publicly.
"""

import os
import re
import secrets
import stat
from contextlib import suppress

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from bud.config import Settings


class OwnerAuthError(Exception):
    """A safe, credential-free configuration error."""


def _validate(token: SecretStr) -> SecretStr:
    # 32 random bytes encoded as base64url produce 43 characters. Operators
    # supplying an override are responsible for generating equivalent entropy.
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", token.get_secret_value()):
        raise OwnerAuthError(
            "OWNER_TOKEN must be a strong base64url secret of 43 to 256 characters."
        )
    return token


def _check_private(fd: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    expected = 0o700 if directory else 0o600
    right_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not right_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != expected:
        raise OwnerAuthError(
            "Owner authentication requires an owned 0700 auth directory and 0600 token file."
        )


def _load_or_create(directory: int) -> SecretStr:
    name = "owner.token"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        # Publish only a complete, fsynced file. Hard-link creation is exclusive:
        # concurrent CLI/server processes all load the winner, never rotate it.
        temporary = f".owner-{secrets.token_hex(16)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_urlsafe(32).encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
        finally:
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        _check_private(stream.fileno(), directory=False)
        raw = stream.read(257)
    try:
        return _validate(SecretStr(raw.decode("ascii")))
    except (UnicodeError, OwnerAuthError):
        raise OwnerAuthError(
            "Invalid owner token file; restore it securely. It will not be replaced automatically."
        ) from None


def owner_token(settings: Settings) -> SecretStr:
    """Load the stable owner secret, or atomically create it in private local state."""
    if settings.owner_token is not None:
        return _validate(settings.owner_token)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = os.open(settings.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(data)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise OwnerAuthError("DATA_DIR must be owned by you and not writable by others.")
            with suppress(FileExistsError):
                os.mkdir("auth", mode=0o700, dir_fd=data)
            directory = os.open("auth", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=data)
            try:
                _check_private(directory, directory=True)
                return _load_or_create(directory)
            finally:
                os.close(directory)
        finally:
            os.close(data)
    except OSError:
        raise OwnerAuthError(
            "Cannot securely load owner authentication from DATA_DIR/auth. "
            "Check ownership, permissions and symlinks; no unauthenticated fallback is allowed."
        ) from None


class OwnerAuthentication:
    """Authenticate before routing, body parsing, session lookup or SSE setup.

    Pure ASGI middleware avoids buffering long-lived SSE responses. Protect the
    entire app (including docs and future endpoints), rather than a route list.
    """

    def __init__(self, app: ASGIApp, token: SecretStr, scope_id: str | None = None) -> None:
        self.app = app
        self.token = token
        self.scope_id = scope_id
        self.bearer = HTTPBearer(auto_error=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        credentials = await self.bearer(request)
        headers = request.headers.getlist("authorization")
        if (
            len(headers) != 1
            or credentials is None
            or len(credentials.credentials) > 256
            or not secrets.compare_digest(
                credentials.credentials.encode("utf-8"),
                self.token.get_secret_value().encode("ascii"),
            )
        ):
            response = JSONResponse(
                {"detail": "Owner authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        expected = request.headers.getlist("x-bud-scope")
        if expected and (len(expected) != 1 or expected[0] != self.scope_id):
            response = JSONResponse(
                {
                    "detail": (
                        "Bud account scope changed. Restart the client; do not reuse old approvals."
                    )
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
