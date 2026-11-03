"""Public surface of the model gateway.
`build_gateway` wires the registry, ledger, and real provider clients into a `Gateway`.
"""

from bud.config import Settings, get_settings
from bud.gateway.ledger import Ledger
from bud.gateway.registry import load_registry
from bud.gateway.router import Gateway, NoModelAvailable, provider_model_factory

__all__ = ["Gateway", "Ledger", "NoModelAvailable", "build_gateway"]


def build_gateway(settings: Settings | None = None) -> Gateway:
    settings = settings or get_settings()
    return Gateway(
        load_registry(),
        Ledger(settings.data_dir / "bud.db"),
        provider_model_factory(settings),
    )
