"""Hosted sign-in and local account references. OAuth tokens stay with Composio."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from composio import SESSION_PRESET_DIRECT_TOOLS, Composio
from composio_client.types.connected_account_retrieve_response import (
    ConnectedAccountRetrieveResponse,
)

from bud.composio.binding import (
    TOOLKITS,
    AccountBinding,
    BindingStore,
    ConnectionError,
    valid_toolkit,
)
from bud.config import Settings

PENDING_STATUSES = {"INITIATED", "INITIALIZING"}
# Composio's hosted link can expire before the connected account status updates.
SIGN_IN_LIFETIME = timedelta(minutes=10)


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
        self._link_lock = Lock()
        self._initial_links: dict[str, tuple[str, str, float]] = {}

    def client(self) -> Composio:
        key = self.settings.composio_api_key
        if key is None or not key.get_secret_value().strip():
            raise ConnectionError("Set COMPOSIO_API_KEY in .env before connecting an account.")
        return Composio(
            api_key=key.get_secret_value(), allow_tracking=False, max_retries=0, timeout=20
        )

    def accounts(self) -> dict[str, str]:
        return self.store.snapshot()[0].accounts

    def pending_url(self, toolkit: str) -> str | None:
        """Recover an unfinished sign-in link without replacing its account reference."""
        binding, pending = self.store.snapshot()
        account_id = pending.get(toolkit)
        if account_id is None:
            if toolkit in binding.accounts:
                raise ConnectionError("This app is already connected.")
            return None
        with safe_errors("Could not recover sign-in. Try again or check the connection."):
            account = self._account(self.client(), binding, toolkit, account_id)
            status = self._account_status(account)
            value = None
            if account is not None and status in PENDING_STATUSES:
                state = account.state.model_dump().get("val", {})
                value = state.get("composio_link_redirect_url") or state.get("redirect_url")
                # Composio can omit the URL while a newly created OAuth link is still unopened.
                if value is None and status == "INITIALIZING":
                    with self._link_lock:
                        cached = self._initial_links.get(toolkit)
                    if cached is not None:
                        cached_id, cached_url, expires_at = cached
                        if cached_id == account_id and monotonic() < expires_at:
                            value = cached_url
            if self.store.snapshot() != (binding, pending):
                raise ConnectionError("Connection changed while recovering sign-in. Try again.")
            if status == "ACTIVE":
                raise ConnectionError("This app is already connected.")
            if value is None:
                return None
            if not isinstance(value, str):
                raise ConnectionError("The sign-in link is unavailable. Check the connection.")
            link = urlsplit(value)
            if link.scheme != "https" or not link.hostname:
                raise ConnectionError("The sign-in link is unavailable. Check the connection.")
            return value

    def connect(self, toolkit: str, *, replace_active: bool = True) -> str:
        if not valid_toolkit(toolkit):
            raise ConnectionError("Unsupported toolkit name.")
        with safe_errors("Could not start Composio sign-in. Check your key and connection."):
            attempt = self.store.begin_connect(toolkit, replace_active=replace_active)
            client = self.client()
            session = client.create(
                user_id=attempt.binding.owner,
                toolkits=[toolkit],
                session_preset=SESSION_PRESET_DIRECT_TOOLS,
                manage_connections=False,
            )
            issued_at = monotonic()
            request = session.authorize(toolkit)
            if not request.redirect_url:
                raise ConnectionError("Composio did not return a sign-in link. Check /connections.")
            with self._link_lock:
                self.store.save_pending(attempt, toolkit, request.id)
                now = monotonic()
                self._initial_links = {
                    app: link for app, link in self._initial_links.items() if link[2] > now
                }
                self._initial_links[toolkit] = (
                    request.id,
                    request.redirect_url,
                    issued_at + SIGN_IN_LIFETIME.total_seconds(),
                )
            return request.redirect_url

    def statuses(self) -> dict[str, str]:
        return self.refresh()[1]

    def disconnect(self, toolkit: str) -> bool:
        with (
            safe_errors("Could not disconnect this app. Check your connections and try again."),
            self._link_lock,
        ):
            removed = self.store.disconnect(toolkit)
            self._initial_links.pop(toolkit, None)
            return removed

    def _account(
        self, client: Composio, binding: AccountBinding, toolkit: str, account_id: str
    ) -> ConnectedAccountRetrieveResponse | None:
        try:
            account = client.connected_accounts.get(nanoid=account_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if (
            account.id != account_id
            or account.user_id != binding.owner
            or binding.owner != self.user_id
            or account.toolkit.slug != toolkit
        ):
            raise ConnectionError("Connection owner or toolkit changed. Reconnect the account.")
        return account

    @staticmethod
    def _account_status(account: ConnectedAccountRetrieveResponse | None) -> str:
        if account is None:
            return "REVOKED"
        if account.status in {"EXPIRED", "REVOKED"}:
            return account.status
        if account.is_disabled:
            return "DISABLED"
        if account.status in PENDING_STATUSES:
            if getattr(getattr(account.state, "val", None), "status", None) == "EXPIRED":
                return "EXPIRED"
            created_at = getattr(account, "created_at", None)
            if created_at:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if datetime.now(UTC) >= created + SIGN_IN_LIFETIME:
                    return "EXPIRED"
        # Only return known metadata, never arbitrary provider error text or URLs.
        known = PENDING_STATUSES | {"ACTIVE", "INACTIVE", "FAILED"}
        return account.status if account.status in known else "UNAVAILABLE"

    def refresh(self) -> tuple[AccountBinding, dict[str, str]]:
        """Verify references, accept completed sign-ins, and fence removed accounts.

        This updates local metadata only. A newly added app is adopted as the current
        binding, so its tools load without a restart; a removal or replacement keeps the
        restart fence.
        """
        binding, statuses, _ = self._refresh()
        return binding, statuses

    def inventory(self) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Refresh and list the app references Bud holds, including retired links."""
        _, statuses, accounts = self._refresh(include_inactive=True)
        return statuses, accounts

    def _refresh(
        self, *, include_inactive: bool = False
    ) -> tuple[AccountBinding, dict[str, str], list[dict[str, str]]]:
        with safe_errors("Could not verify connections. No account tools were refreshed."):
            if include_inactive:
                previous, pending, inactive = self.store.inventory()
            else:
                previous, pending = self.store.snapshot()
                inactive = {}
            references = previous.accounts | {k: v for k, v in pending.items() if v is not None}
            inventory = inactive | references
            statuses = dict.fromkeys(set(TOOLKITS) | set(references), "NOT_CONNECTED")
            unverifiable: set[str] = set()
            accounts = []
            try:
                if inventory:
                    client = self.client()
                    for toolkit, account_id in sorted(inventory.items()):
                        account = None
                        try:
                            account = self._account(client, previous, toolkit, account_id)
                            status = self._account_status(account)
                        except Exception as exc:
                            if toolkit in references and (
                                toolkit in previous.accounts
                                or getattr(exc, "status_code", None) in (401, 403)
                            ):
                                raise
                            status = "UNAVAILABLE"
                            unverifiable.add(toolkit)
                        if toolkit in references:
                            statuses[toolkit] = status
                        elif status == "ACTIVE":
                            status = "DISCONNECTED"
                        if include_inactive:
                            name = {"gmail": "Gmail", "googlecalendar": "Google Calendar"}.get(
                                toolkit, toolkit.replace("_", " ").title()
                            )
                            accounts.append(
                                {
                                    "toolkit": toolkit,
                                    "account_id": account_id,
                                    "display_name": getattr(account, "alias", None) or name,
                                    "status": "connected" if status == "ACTIVE" else status.lower(),
                                }
                            )
            except Exception:
                if references:
                    self.store.retire(previous)
                raise
            for toolkit, account_id in pending.items():
                if account_id is None:
                    statuses[toolkit] = "CONNECT_REQUESTED"
            active = {k: v for k, v in references.items() if statuses[k] == "ACTIVE"}
            remaining = {
                k: v
                for k, v in pending.items()
                if statuses[k] != "ACTIVE" and k not in unverifiable
            }
            accepted = self.store.accept_verified(
                previous,
                pending,
                active,
                remaining,
                inactive=inactive if include_inactive else None,
            )
            # Adding an app cannot redirect an approval that already exists: every earlier
            # account is unchanged. Adopt it so the new app's tools work in this session.
            # A removal or replacement keeps the restart fence.
            if self.binding is not None and self._additive(self.binding, accepted):
                self.binding = accepted
            return accepted, statuses, accounts

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

    def stored(self) -> AccountBinding:
        """The binding the store holds now, which may differ from the adopted one."""
        current = self.store.snapshot()[0]
        if current.owner != self.user_id:
            raise ConnectionError("Owner changed. Restart with the original owner state.")
        return current

    def adopt(self, expected: AccountBinding) -> None:
        """Take `expected` as current, even after a replacement or removal, but only while the
        store still holds exactly it. The caller builds the new tools for `expected` first, so
        a failure before this leaves the old binding, and the next request tries again. Tools
        built before keep their own binding, which the dispatch fence refuses."""
        if self.stored() != expected:
            raise ConnectionError("Accounts changed while loading tools. Try again.")
        self.binding = expected

    def restart_required(self) -> bool:
        return self.binding is None or self.store.snapshot()[0] != self.binding

    @staticmethod
    def _additive(old: AccountBinding, new: AccountBinding) -> bool:
        """A new app was added and no existing account changed or left."""
        return (
            old.owner == new.owner
            and old.generation == new.generation
            and all(
                new.accounts.get(toolkit) == account for toolkit, account in old.accounts.items()
            )
        )

    def _fenced(self, binding: AccountBinding, current: AccountBinding) -> bool:
        """True when an account this toolset holds was removed or replaced."""
        return current != binding and not self._additive(binding, current)

    def assert_current_binding(self, binding: AccountBinding, target_toolkit: str) -> None:
        """Fence EVERY remote dispatch, including internal verification/read-back calls.

        A remote call already in flight cannot be undone. A failed write must not
        be retried automatically; inspect its outcome before any replacement action.
        """
        with safe_errors("Could not verify the account binding. No new tool call is authorized."):
            current = self.store.snapshot()[0]
            if binding.owner != self.user_id or current.owner != binding.owner:
                raise ConnectionError("Account binding changed. Restart bud and use a new session.")
            account_id = binding.accounts.get(target_toolkit)
            if not valid_toolkit(target_toolkit) or account_id is None:
                raise ConnectionError("App not connected. Connect it first.")
            # Adding another app leaves every earlier account in place, so a call to one
            # of them is still authorized. A removal or replacement keeps the fence.
            if self._fenced(binding, current):
                raise ConnectionError("Account binding changed. Restart bud and use a new session.")
            try:
                account = self._account(self.client(), binding, target_toolkit, account_id)
                status = self._account_status(account)
            except Exception:
                self.store.retire(binding)
                raise
            if status != "ACTIVE":
                self.store.retire(binding, disconnected=target_toolkit)
                raise ConnectionError(
                    "Account is not active. Reconnect, restart and use a new session."
                )
            if self._fenced(binding, self.store.snapshot()[0]):
                raise ConnectionError(
                    "Account changed during verification. Restart with a new session."
                )
