import socket

import pytest

from bud.web import _public_url


@pytest.mark.parametrize(
    "host,addresses",
    [
        ("127.1", ["127.0.0.1"]),
        ("0x7f.0.0.1", ["127.0.0.1"]),
        ("127.0.0.1.nip.io", ["127.0.0.1"]),
        ("private.example", ["10.0.0.1"]),
        ("mixed.example", ["1.1.1.1", "::ffff:127.0.0.1"]),
        ("link.example", ["169.254.169.254"]),
        ("multicast.example", ["224.0.0.1"]),
        ("empty.example", []),
    ],
)
def test_private_or_mixed_dns_answers_are_refused(monkeypatch, host, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses
        ],
    )
    with pytest.raises(ValueError):
        _public_url(f"https://{host}/")


def test_unresolved_host_is_refused_and_public_host_is_accepted(monkeypatch):
    def unresolved(*args, **kwargs):
        raise socket.gaierror("offline test")

    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    with pytest.raises(ValueError, match="could not be resolved"):
        _public_url("https://public.example/")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))],
    )
    _public_url("https://public.example/")
