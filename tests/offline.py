import ipaddress
import socket
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from pydantic_ai import models


@contextmanager
def offline_requests():
    """Allow loopback traffic and scripted models, never external requests."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    getaddrinfo, sendto = socket.getaddrinfo, socket.socket.sendto

    def check_host(host):
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError(f"Tests cannot connect to external address {host}")

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])

    def local_getaddrinfo(host, *args, **kwargs):
        if host is not None:
            check_host(host)
        return getaddrinfo(host, *args, **kwargs)

    def local_connect(sock, address):
        check(sock, address)
        return connect(sock, address)

    def local_connect_ex(sock, address):
        check(sock, address)
        return connect_ex(sock, address)

    def local_sendto(sock, data, *args):
        check(sock, args[-1])
        return sendto(sock, data, *args)

    with ExitStack() as stack:
        stack.enter_context(models.override_allow_model_requests(False))
        stack.enter_context(patch.object(socket.socket, "connect", local_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", local_connect_ex))
        stack.enter_context(patch.object(socket.socket, "sendto", local_sendto))
        stack.enter_context(patch.object(socket, "getaddrinfo", local_getaddrinfo))
        yield
