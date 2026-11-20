"""Hosted sign-in and local account references. OAuth tokens stay with Composio."""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from composio import SESSION_PRESET_DIRECT_TOOLS, Composio

from bud.composio.binding import (
    TOOLKITS,
    AccountBinding,
    BindingStore,
    ConnectionError,
    valid_toolkit,
)
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

    def pending_url(self, toolkit: str) -> str | None:
        """Recover an unfinished sign-in link without replacing its account reference."""
        binding, pending = self.store.snapshot()
        account_id = pending.get(toolkit)
        if account_id is None:
            if toolkit in binding.accounts:
                raise ConnectionError("This app is already connected.")
            return None
        with safe_errors("Could not recover sign-in. Try again or check the connection."):
            account = self.client().connected_accounts.get(nanoid=account_id)
            if (
                account.id != account_id
                or account.user_id != binding.owner
                or binding.owner != self.user_id
                or account.toolkit.slug != toolkit
            ):
                raise ConnectionError("Connection owner or toolkit changed. Reconnect the account.")
            if account.status == "ACTIVE":
                raise ConnectionError("This app is already connected.")
            if account.is_disabled or account.status not in {"INITIATED", "INITIALIZING"}:
                return None
            value = account.state.model_dump().get("val", {}).get("redirect_url")
            if not isinstance(value, str) or urlsplit(value).scheme != "https":
                raise ConnectionError("The sign-in link is unavailable. Check the connection.")
            if self.store.snapshot() != (binding, pending):
                raise ConnectionError("Connection changed while recovering sign-in. Try again.")
            return value

    def connect(self, toolkit: str) -> str:
        if not valid_toolkit(toolkit):
            raise ConnectionError("Unsupported toolkit name.")
        with safe_errors("Could not start Composio sign-in. Check your key and connection."):
            attempt = self.store.begin_connect(toolkit)
            client = self.client()
            session = client.create(
                user_id=attempt.binding.owner,
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

        This updates local metadata only. A newly added app is adopted as the current
        binding, so its tools load without a restart; a removal or replacement keeps the
        restart fence.
        """
        with safe_errors("Could not verify connections. No account tools were refreshed."):
            previous, pending = self.store.snapshot()
            references = previous.accounts | {k: v for k, v in pending.items() if v is not None}
            statuses = dict.fromkeys(set(TOOLKITS) | set(references), "NOT_CONNECTED")
            unverifiable: set[str] = set()
            try:
                if references:
                    client = self.client()
                    for toolkit, account_id in references.items():
                        try:
                            statuses[toolkit] = self._status(client, previous, toolkit, account_id)
                        except Exception as exc:
                            if toolkit in previous.accounts or getattr(
                                exc, "status_code", None
                            ) in (401, 403):
                                raise  # an active account in doubt, or a rejected key
                            # Only an unfinished sign-in is in doubt; it never became an
                            # account, so it is dropped below without fencing the chat.
                            statuses[toolkit] = "UNAVAILABLE"
                            unverifiable.add(toolkit)
            except Exception:
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
            accepted = self.store.accept_verified(previous, pending, active, remaining)
            # Adding an app cannot redirect an approval that already exists: every earlier
            # account is unchanged. Adopt it so the new app's tools work in this session.
            # A removal or replacement keeps the restart fence.
            if self.binding is not None and self._additive(self.binding, accepted):
                self.binding = accepted
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

    def adopt(self) -> tuple[AccountBinding | None, AccountBinding]:
        """Take the stored binding as current, even after a replacement or removal, and
        return (before, after). The caller rebuilds account tools and denies approvals given
        under an account that changed; tools built before keep their own binding, which the
        dispatch fence refuses."""
        before = self.binding
        current = self.store.snapshot()[0]
        if current.owner != self.user_id:
            raise ConnectionError("Owner changed. Restart with the original owner state.")
        self.binding = current
        return before, current

    @staticmethod
    def changed_apps(before: AccountBinding | None, after: AccountBinding) -> set[str]:
        """Apps whose account was replaced or removed between two bindings."""
        if before is None:
            return set()
        return {
            toolkit
            for toolkit, account in before.accounts.items()
            if after.accounts.get(toolkit) != account
        }

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
                status = self._status(self.client(), binding, target_toolkit, account_id)
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
