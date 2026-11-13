"""X-Forwarded-For hops our proxy writes in a form ipaddress cannot parse (review finding: 'An X-Forwarded-For entry the
app cannot parse lets the client pick its bucket key'). Fixed in app/security/net.py (``_hop``).
Each case failed before: the right-to-left walk skipped the unreadable hop and continued into what the client wrote."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.security.net import client_ip_from_scope


@pytest.fixture
def behind_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxies", ["10.0.0.0/8", "127.0.0.1"])


def _scope(xff: str, real_ip: str | None = None) -> dict:
    headers = [(b"x-forwarded-for", xff.encode())]
    if real_ip:
        headers.append((b"x-real-ip", real_ip.encode()))
    return {"type": "http", "client": ("10.0.0.5", 4000), "headers": headers}


@pytest.mark.parametrize("xff,expected", [
    ("6.6.6.6, 198.51.100.7:51234", "198.51.100.7"),  # Azure App Service / IIS ARR write ip:port
    ("6.6.6.6, [2001:db8::7]:443", "2001:db8::7"),
    ("6.6.6.6, [2001:db8::8]", "2001:db8::8"),
    ("6.6.6.6, 2001:db8::9", "2001:db8::9"),
    ("6.6.6.6, unknown", "10.0.0.5"),  # unreadable: the peer, never the client-written 6.6.6.6
    ("6.6.6.6, unix:", "10.0.0.5"),
    ("6.6.6.6, 198.51.100.7:http", "10.0.0.5"),
    ("6.6.6.6, 198.51.100.7, 10.0.0.9", "198.51.100.7"),  # our own proxies are still skipped
    ("6.6.6.6, 198.51.100.7:51234, 10.0.0.9:8080", "198.51.100.7"),
])
def test_an_unreadable_hop_never_hands_the_key_to_the_client(behind_proxy: None, xff: str, expected: str) -> None:
    assert client_ip_from_scope(_scope(xff)) == expected


def test_garbage_plus_a_client_written_real_ip_is_the_peer(behind_proxy: None) -> None:
    assert client_ip_from_scope(_scope("garbage", real_ip="6.6.6.7")) == "10.0.0.5"


def test_all_our_proxies_falls_back_to_x_real_ip(behind_proxy: None) -> None:
    assert client_ip_from_scope(_scope("10.0.0.7, 10.0.0.8", real_ip="198.51.100.10")) == "198.51.100.10"
