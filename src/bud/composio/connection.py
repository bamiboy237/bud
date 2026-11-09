"""Hosted sign-in and local account references. OAuth tokens stay with Composio."""

from collections.abc import Iterator
from contextlib import contextmanager

from composio import SESSION_PRESET_DIRECT_TOOLS, Composio

from bud.composio.binding import TOOLKITS, AccountBinding, BindingStore, ConnectionError
from bud.config import Settings


@contextmanager
def safe_errors(message: str) -> Iterator[None]:
    try:
        yield
    except ConnectionError:
        raise
    except Exception as exc:
        if getattr(exc, "status_code", None) == 401:
            message = "Composio rejected the API key. Set a valid COMPOSIO_API_KEY in .env."
        # SDK exceptions can contain request headers or credential-bearing URLs.
        raise ConnectionError(message) from None


class Connections:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.folder = settings.data_dir / "composio"
        self.path = self.folder / "connections.db"
        with safe_errors("Could not read the local owner and account binding."):
            self.store = BindingStore(self.path)
            self.user_id = self.store.snapshot()[0].owner
        self.binding: AccountBinding | None = None

    def client(self) -> Composio:
        key = self.settings.composio_api_key
        if key is None or not key.get_secret_value().strip():
            raise ConnectionError("Set COMPOSIO_API_KEY in .env before connecting an account.")
        return Composio(
            api_key=key.get_secret_value(), allow_tracking=False, max_retries=0, timeout=20
        )

    def accounts(self) -> dict[str, str]:
        return self.store.snapshot()[0].accounts

    def connect(self, toolkit: str) -> str:
        if toolkit not in TOOLKITS:
            raise ConnectionError("Unsupported toolkit. Use gmail or calendar.")
        with safe_errors("Could not start Composio sign-in. Check your key and connection."):
            attempt = self.store.begin_connect(toolkit)
            client = self.client()
            session = client.create(
                user_id=attempt.owner,
                toolkits=[toolkit],
                session_preset=SESSION_PRESET_DIRECT_TOOLS,
                manage_connections=False,
            )
            request = session.authorize(toolkit)
            if not request.redirect_url:
                raise ConnectionError("Composio did not return a sign-in link. Check /connections.")
            self.store.save_pending(attempt, toolkit, request.id)
            return request.redirect_url

    def statuses(self) -> dict[str, str]:
        return self.refresh()[1]

    def _status(
        self, client: Composio, binding: AccountBinding, toolkit: str, account_id: str
    ) -> str:
        account = client.connected_accounts.get(nanoid=account_id)
        if (
            account.id != account_id
            or account.user_id != binding.owner
            or account.toolkit.slug != toolkit
        ):
            raise ConnectionError("Connection owner or toolkit changed. Reconnect the account.")
        if account.is_disabled:
            return "DISABLED"
        # Only return known metadata, never arbitrary provider error text or URLs.
        known = {
            "ACTIVE",
            "INITIATED",
            "INITIALIZING",
            "INACTIVE",
            "FAILED",
            "EXPIRED",
            "REVOKED",
        }
        return account.status if account.status in known else "UNAVAILABLE"

    def refresh(self) -> tuple[AccountBinding, dict[str, str]]:
        """Verify references, accept completed sign-ins, and fence removed accounts.

        This updates local metadata only. It never replaces a running toolset/DBOS
        instance; a full process restart is the supported refresh boundary.
        """
        with safe_errors("Could not verify connections. No account tools were refreshed."):
            previous, pending = self.store.snapshot()
            references = previous.accounts | {k: v for k, v in pending.items() if v is not None}
            statuses = dict.fromkeys(TOOLKITS, "NOT_CONNECTED")
            try:
                if references:
                    client = self.client()
                    for toolkit, account_id in references.items():
                        statuses[toolkit] = self._status(client, previous, toolkit, account_id)
            except Exception:
                self.store.retire(previous)
                raise
            for toolkit, account_id in pending.items():
                if account_id is None:
                    statuses[toolkit] = "CONNECT_REQUESTED"
            active = {k: v for k, v in references.items() if statuses[k] == "ACTIVE"}
            remaining = {k: v for k, v in pending.items() if statuses[k] != "ACTIVE"}
            accepted = self.store.accept_verified(previous, pending, active, remaining)
            return accepted, statuses

    def capture_binding(self) -> AccountBinding:
        if self.binding is not None:
            if self.restart_required():
                raise ConnectionError("Account binding changed. Restart with a new session.")
            return self.binding
        binding = self.refresh()[0]
        if binding.owner != self.user_id:
            raise ConnectionError(
                "Owner changed during startup. Restart with the original owner state."
            )
        self.binding = binding
        return self.binding

    def restart_required(self) -> bool:
        return self.binding is None or self.store.snapshot()[0] != self.binding

    def assert_current_binding(self, binding: AccountBinding, target_toolkit: str) -> None:
        """Fence EVERY remote dispatch, including internal verification/read-back calls.

        A remote call already in flight cannot be undone. A failed write must not
        be retried automatically; inspect its outcome before any replacement action.
        """
        with safe_errors("Could not verify the account binding. No new tool call is authorized."):
            current = self.store.snapshot()[0]
            if self.binding != binding or binding.owner != self.user_id or current != binding:
                raise ConnectionError("Account binding changed. Restart bud and use a new session.")
            account_id = binding.accounts.get(target_toolkit)
            if target_toolkit not in TOOLKITS or account_id is None:
                raise ConnectionError("App not connected. Use /connect gmail or /connect calendar.")
            try:
                status = self._status(self.client(), binding, target_toolkit, account_id)
            except Exception:
                self.store.retire(binding)
                raise
            if status != "ACTIVE":
                self.store.retire(binding, disconnected=target_toolkit)
                raise ConnectionError(
                    "Account is not active. Reconnect, restart and use a new session."
                )
            if self.store.snapshot()[0] != binding:
                raise ConnectionError(
                    "Account changed during verification. Restart with a new session."
                )
