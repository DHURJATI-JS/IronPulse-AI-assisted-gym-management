"""Client address helpers.

Proxy headers are honoured only when TRUST_PROXY_HEADERS=true *and* the request arrives from one of
TRUSTED_PROXIES (addresses or CIDR ranges of your own reverse proxies). Each proxy appends the address it
received the request from to X-Forwarded-For, so the left-most entries are whatever the client typed: the real
client is the right-most address that is not one of our proxies. Taking the first entry would let anyone pick
their IP - dodging bans and rate limits, claiming a whitelisted address, or getting someone else's IP banned.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any

from app.config import get_settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _clean(ip: str | None) -> str:
    if not ip:
        return "0.0.0.0"
    ip = ip.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return "0.0.0.0"


@lru_cache(maxsize=16)
def _networks(entries: tuple[str, ...]) -> tuple[Network, ...]:
    out = []
    for entry in entries:
        try:
            out.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            continue
    return tuple(out)


def is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _networks(tuple(get_settings().trusted_proxies)))


def _hop(entry: str) -> str | None:
    """One X-Forwarded-For entry as an address: ``a.b.c.d``, ``a.b.c.d:port`` (Azure App Service, IIS ARR),
    ``[v6]``, ``[v6]:port`` or a bare IPv6 address; None for anything else (``unknown``, ``unix:``, garbage)."""
    text = entry.strip()
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1:] if end > 0 else "x"
        if end <= 0 or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return None
        host = text[1:end]
    elif text.count(":") == 1:  # IPv4 with a port (a bare IPv6 address has at least two colons)
        host, port = text.split(":")
        if not port.isdigit():
            return None
    else:
        host = text
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def client_ip_from_scope(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    peer = _clean(client[0] if client else None)
    if not get_settings().trust_proxy_headers or not is_trusted_proxy(peer):
        return peer  # direct connection (or proxy headers disabled): the TCP peer is the client
    forwarded = [v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"x-forwarded-for"]
    entries = [part for value in forwarded for part in value.split(",") if part.strip()]
    for entry in reversed(entries):  # right to left: skip our own proxies, the first other address is the client
        addr = _hop(entry)
        if addr is None:  # a hop our proxy wrote in a form we cannot read: never walk on into what the client wrote
            return peer
        if not is_trusted_proxy(addr):
            return addr
    real_ip = header(scope, b"x-real-ip")  # set by the proxy itself (overwritten, not appended)
    if real_ip and _clean(real_ip) != "0.0.0.0":
        return _clean(real_ip)
    return peer


def client_ip(request: Any) -> str:
    return client_ip_from_scope(request.scope)


def header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


# ---------------------------------------------------------------- who made the client send this request
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CROSS_SITE, SAME_ORIGIN, UNKNOWN = "cross-site", "same-origin", "unknown"


def origin_allowed(origin: str, host: str | None) -> bool:
    """An ``Origin`` header value (not "null") of this app: the requested host over http(s), PUBLIC_BASE_URL or one of
    CORS_ORIGINS."""
    settings = get_settings()
    host = host or ""
    allowed = {f"http://{host}", f"https://{host}", settings.public_base_url, *settings.cors_origins}
    return origin.strip().rstrip("/") in {a.rstrip("/") for a in allowed if a}


def request_site(scope: dict[str, Any]) -> str:
    """Who made the client send this request - from what browsers set themselves, which no web page can forge:

    * CROSS_SITE: a browser sent it for something other than this app's own pages - another site's page (an <img>, a
      form, ``fetch(url, {mode: "no-cors"})``), a link in an e-mail or a chat app, a bookmark (``Sec-Fetch-Site``
      cross-site / same-site / none), or a page whose Origin is not ours ("null": sandboxed or opaque pages);
    * SAME_ORIGIN: this app's own pages (``Sec-Fetch-Site: same-origin`` or our Origin);
    * UNKNOWN: no browser metadata at all - a script or a tool (or a very old browser) sent it itself."""
    origin = header(scope, b"origin")
    if origin is not None:
        origin = origin.strip()
        if origin == "null" or not origin_allowed(origin, header(scope, b"host")):
            return CROSS_SITE
    site = (header(scope, b"sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site", "none"):
        return CROSS_SITE
    if site == "same-origin" or origin is not None:
        return SAME_ORIGIN
    return UNKNOWN


def attributable(scope: dict[str, Any], method: str) -> bool:
    """Whether the URL and body of this request may be held against its network address (threat points, IP bans).

    Not when a browser was made to send them: any web page, e-mail or chat message can make a visitor's browser request
    any URL of this app (``<img src=".../?f=/etc/passwd">``) or post any form to it - from another site (a foreign
    Origin / ``Sec-Fetch-Site``), and for a GET even from this app's own pages (a link or image in user content can point
    anywhere). Banning on that evidence let any third party lock a visitor - and everyone behind the same gym Wi-Fi or
    carrier NAT - out of the app. Such requests are still refused when they match an attack signature, and logged.

    Attributable: requests without browser metadata (scripts, tools, very old browsers) and this app's own pages'
    POST / PUT / PATCH / DELETE. A tool can add the headers too - that only spares its own address the ban; every
    matching request is still refused, and a scanner User-Agent always counts (a page can't set a browser's)."""
    site = request_site(scope)
    if site == UNKNOWN:
        return True
    if site == CROSS_SITE:
        return False
    return (method or "GET").upper() not in SAFE_METHODS
