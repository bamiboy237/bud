import socket

import pytest
from pydantic_ai import models

from bud.config import get_settings


def test_tests_do_not_inherit_application_credentials_or_state(tmp_path):
    settings = get_settings()
    assert settings.data_dir == tmp_path.resolve() / "data"
    for name in type(settings).model_fields:
        if name.endswith("_api_key") or name in {"owner_token", "model_api_keys"}:
            assert not getattr(settings, name), f"Test inherited setting {name}"
    assert not settings.approval_review
    assert not settings.loop_detection
    assert models.ALLOW_MODEL_REQUESTS is False


def test_outbound_dns_and_connections_are_blocked():
    with pytest.raises(AssertionError, match="external address"):
        socket.getaddrinfo("model.example", 443)
    with socket.socket() as connection, pytest.raises(AssertionError, match="external address"):
        connection.connect(("192.0.2.1", 443))
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection,
        pytest.raises(AssertionError, match="external address"),
    ):
        connection.sendto(b"offline test", ("192.0.2.1", 443))
