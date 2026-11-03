"""No real network in tests/e2e_ob: every socket connection or name lookup to anything but this PC (the test client's own
loopback pair) is refused and recorded, and the DNS resolver is refused too. The invariant sweep asserts that nothing
was even tried."""

from __future__ import annotations

import socket

import dns.exception
import dns.resolver
import pytest

LOCAL = {"127.0.0.1", "::1", "localhost", None, ""}


@pytest.fixture(autouse=True)
def network_attempts(monkeypatch):
    attempts: list = []
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def _host(address):
        return address[0] if isinstance(address, tuple) else address

    def connect(self, address):
        if _host(address) in LOCAL:
            return real_connect(self, address)
        attempts.append(("connect", address))
        raise OSError("real network blocked in tests/e2e_ob")

    def connect_ex(self, address):
        if _host(address) in LOCAL:
            return real_connect_ex(self, address)
        attempts.append(("connect", address))
        return 111

    def getaddrinfo(host, *a, **k):
        if host in LOCAL:
            return real_gai(host, *a, **k)
        attempts.append(("dns", host))
        raise socket.gaierror(11001, "name lookups blocked in tests/e2e_ob")

    def resolve(self, qname, *a, **k):
        attempts.append(("dns-resolver", str(qname)))
        raise dns.exception.Timeout()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    return attempts
