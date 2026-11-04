"""Public surface of the model gateway.
`build_gateway` wires the registry, ledger, and real provider clients into a `Gateway`.
"""

from bud.config import Settings, get_settings
from bud.gateway.ledger import Ledger
from bud.gateway.registry import Registry, load_registry
from bud.gateway.router import Gateway, NoModelAvailable, provider_model_factory

__all__ = ["Gateway", "Ledger", "NoModelAvailable", "build_gateway"]


def build_gateway(
    settings: Settings | None = None, *, providers: set[str] | None = None
) -> Gateway:
    settings = settings or get_settings()
    registry = load_registry()
    if providers is not None:
        registry = Registry(models=[spec for spec in registry.models if spec.provider in providers])
    return Gateway(
        registry,
        Ledger(settings.data_dir / "bud.db"),
        provider_model_factory(settings),
    )
