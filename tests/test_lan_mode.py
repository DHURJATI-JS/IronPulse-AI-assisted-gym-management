"""LAN mode - IronPulse on phones and other devices of the local network, HTTPS only (scripts/lan.py, ./run.sh --lan,
run.ps1 -Lan, README "Use it on your phone (LAN)").

* certificates: a name-constrained local CA (made once) and a server certificate for exactly the LAN address(es),
  <name>.local and localhost; keys 0600 in a 0700 folder; reused while they fit, renewed when the LAN address changes,
  near expiry, after a new CA or a broken key; a TLS client trusting only the CA accepts those names and nothing else,
  and the CA cannot vouch for a public website;
* the environment it runs with (never .env): debug/reload off, Secure cookies, HTTPS forced, proxy headers off,
  ALLOWED_HOSTS / origins exactly the LAN names, empty CORS and rate-limit whitelist - and the production checks pass;
* a real server end to end (subprocess, its own port): TLS 1.2+ with forward-secret AEAD ciphers only, any other Host
  refused, no API docs, __Host- Secure cookies, HSTS without includeSubDomains (none for localhost), the CSRF and
  WebSocket origin checks accepting only LAN origins;
* run.sh --lan / run.ps1 -Lan, the dry run, the port check, mkcert, phone steps and the QR code.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import ipaddress
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.x509.oid import ExtendedKeyUsageOID

ROOT = Path(__file__).resolve().parent.parent
IP = "192.168.50.23"
NAME = "testmac.local"


def _load_lan() -> Any:
    key = "ironpulse_scripts_lan"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / "lan.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


lan = _load_lan()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ===================================================================================== certificates
def test_certificates_cover_exactly_the_lan_names_with_safe_files(tmp_path):
    tls = tmp_path / "tls"
    state = lan.ensure_certificates(tls, [NAME, "localhost"], [IP])
    assert state.ca_created and state.leaf_created and state.reason == "new"
    leaf = x509.load_pem_x509_certificate(state.leaf_cert.read_bytes())
    ca = x509.load_pem_x509_certificate(state.ca_cert.read_bytes())
    assert lan.cert_names(leaf) == (["localhost", NAME], [IP])
    leaf.verify_directly_issued_by(ca)
    assert leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH])
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days <= 398  # what Apple and browsers accept
    basic = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca and basic.value.path_length == 0
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    permitted = {str(p.value) for p in constraints.permitted_subtrees}
    assert {"local", "localhost", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"} <= permitted
    assert not any(ipaddress.ip_network(p).is_global for p in permitted if "/" in p)  # nothing on the internet
    # files: the folder 0700, keys 0600, certificates readable
    assert mode(tls) == 0o700
    assert mode(tls / lan.CA_KEY) == 0o600 and mode(state.leaf_key) == 0o600
    assert mode(state.ca_cert) == 0o644 and mode(state.leaf_cert) == 0o644
    assert state.ca_der.read_bytes() == ca.public_bytes(serialization_der())


def serialization_der() -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.Encoding.DER


def test_certificates_are_reused_and_renewed_only_when_needed(tmp_path):
    tls = tmp_path / "tls"
    first = lan.ensure_certificates(tls, [NAME, "localhost"], [IP])
    ca_bytes, leaf_bytes = first.ca_cert.read_bytes(), first.leaf_cert.read_bytes()

    again = lan.ensure_certificates(tls, [NAME, "localhost"], [IP])
    assert not again.ca_created and not again.leaf_created and first.leaf_cert.read_bytes() == leaf_bytes

    moved = lan.ensure_certificates(tls, [NAME, "localhost"], ["192.168.50.99"])  # DHCP gave a new address
    assert moved.leaf_created and moved.reason == "the LAN address or name changed" and not moved.ca_created
    assert first.ca_cert.read_bytes() == ca_bytes  # the phones keep trusting the same CA
    leaf = x509.load_pem_x509_certificate(moved.leaf_cert.read_bytes())
    assert lan.cert_names(leaf) == (["localhost", NAME], ["192.168.50.99"])

    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=lan.LEAF_DAYS - lan.RENEW_DAYS + 1)
    renewed = lan.ensure_certificates(tls, [NAME, "localhost"], ["192.168.50.99"], now=later)
    assert renewed.leaf_created and renewed.reason == "it was about to expire"

    (tls / lan.LEAF_KEY).write_text("not a key")  # a broken key: a new pair, never a mismatched one
    broken = lan.ensure_certificates(tls, [NAME, "localhost"], ["192.168.50.99"])
    assert broken.leaf_created and broken.reason == "new"

    (tls / lan.CA_KEY).unlink()  # the CA key is gone: a new CA, and a server certificate it signed
    fresh = lan.ensure_certificates(tls, [NAME, "localhost"], ["192.168.50.99"])
    assert fresh.ca_created and fresh.leaf_created and fresh.reason == "the CA changed"
    assert fresh.ca_cert.read_bytes() != ca_bytes

    forced = lan.ensure_certificates(tls, [NAME, "localhost"], ["192.168.50.99"], force=True)
    assert forced.leaf_created and forced.reason == "renewed on request" and not forced.ca_created


def _handshake(server: ssl.SSLContext, client: ssl.SSLContext, hostname: str) -> None:
    """A TLS handshake in memory (no sockets); raises ssl.SSLError when the client refuses the certificate."""
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname=hostname)
    s = server.wrap_bio(s_in, s_out, server_side=True)
    done_c = done_s = False
    for _ in range(20):
        for side, flag in ((c, "c"), (s, "s")):
            if (flag == "c" and done_c) or (flag == "s" and done_s):
                continue
            try:
                side.do_handshake()
                if flag == "c":
                    done_c = True
                else:
                    done_s = True
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if done_c and done_s:
            return
    raise AssertionError("handshake did not finish")


def test_a_client_trusting_only_the_ca_accepts_exactly_these_names(tmp_path):
    state = lan.ensure_certificates(tmp_path / "tls", [NAME, "localhost"], [IP])
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(state.leaf_cert, state.leaf_key)
    client = ssl.create_default_context(cafile=str(state.ca_cert))
    for good in (NAME, "localhost", IP):
        _handshake(server, client, good)
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(server, client, "evil.example")
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(server, client, "192.168.50.24")


def test_a_stolen_ca_key_cannot_vouch_for_a_public_website(tmp_path):
    """The CA is name-constrained: a certificate it signs for www.example.com (or a public IP) is rejected by a
    client that trusts the CA - the risk of installing it on a phone stays inside the local network."""
    from cryptography.hazmat.primitives import serialization

    state = lan.ensure_certificates(tmp_path / "tls", [NAME, "localhost"], [IP])
    ca = x509.load_pem_x509_certificate(state.ca_cert.read_bytes())
    ca_key = serialization.load_pem_private_key((tmp_path / "tls" / lan.CA_KEY).read_bytes(), password=None)
    client = ssl.create_default_context(cafile=str(state.ca_cert))
    for names, ips, sni in ((["www.example.com"], [], "www.example.com"), ([NAME], ["8.8.8.8"], "8.8.8.8")):
        cert, key = lan._make_leaf(ca, ca_key, names, ips, dt.datetime.now(dt.UTC))
        forged = tmp_path / "forged.pem"
        forged.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + lan._pem_key(key))
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(forged)
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(server, client, sni)


def test_mkcert_is_used_only_when_asked(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        if cmd[1:] == ["-CAROOT"]:
            return SimpleNamespace(returncode=0, stdout=str(tmp_path / "mkcert-root") + "\n", stderr="")
        Path(cmd[cmd.index("-key-file") + 1]).write_text("key")
        Path(cmd[cmd.index("-cert-file") + 1]).write_text("cert")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lan.shutil, "which", lambda name: "/opt/bin/mkcert" if name == "mkcert" else None)
    state = lan.mkcert_certificates(tmp_path / "tls", [NAME, "localhost"], [IP], run=fake_run)
    assert calls[0][:2] == ["/opt/bin/mkcert", "-ecdsa"] and calls[0][-3:] == [NAME, "localhost", IP]
    assert "-install" not in calls[0]  # never touches a trust store
    assert state.ca_cert == tmp_path / "mkcert-root" / "rootCA.pem" and mode(state.leaf_key) == 0o600
    monkeypatch.setattr(lan.shutil, "which", lambda name: None)
    with pytest.raises(lan.LanError, match="mkcert is not installed"):
        lan.mkcert_certificates(tmp_path / "tls", [NAME], [IP], run=fake_run)


# ===================================================================================== the environment
def test_the_environment_is_exactly_the_lan_names_and_secure():
    env = lan.lan_env(ips=[IP], hostname=NAME, port=11000)
    assert env == {
        "HOST": "0.0.0.0", "PORT": "11000", "DEBUG": "false", "RELOAD": "false", "COOKIE_SECURE": "true",
        "FORCE_HTTPS": "true", "TRUST_PROXY_HEADERS": "false", "ALLOWED_HOSTS": f"{IP},{NAME},localhost",
        "PUBLIC_BASE_URL": f"https://{NAME}:11000", "CORS_ORIGINS": "", "RATE_LIMIT_WHITELIST": "",
        "IRONPULSE_LAN": "1", "IRONPULSE_WORKERS": "1",
    }
    many = lan.lan_env(ips=[IP, "10.0.0.7"], hostname=NAME, port=11000, workers=3)
    assert many["ALLOWED_HOSTS"] == f"{IP},10.0.0.7,{NAME},localhost"
    assert many["REALTIME_PG_BRIDGE"] == "true" and many["IRONPULSE_WORKERS"] == "3"
    assert lan.lan_origins([IP], NAME, 11000) == [f"https://{IP}:11000", f"https://{NAME}:11000", "https://localhost:11000"]


def lan_settings(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Any:
    from app.config import build_settings

    for key, value in {**lan.lan_env(ips=[IP], hostname=NAME, port=11000), **extra}.items():
        monkeypatch.setenv(key, value)
    return build_settings()


def test_the_settings_and_origin_checks_follow_the_environment(monkeypatch):
    from app.routers import core
    from app.security import csrf

    s = lan_settings(monkeypatch)
    assert (s.cookie_secure, s.force_https, s.trust_proxy_headers, s.debug, s.reload) == (True, True, False, False, False)
    assert s.allowed_hosts == [IP, NAME, "localhost"] and s.cors_origins == [] and s.rate_limit_whitelist == []
    monkeypatch.setattr(csrf, "get_settings", lambda: s)
    monkeypatch.setattr(core, "get_settings", lambda: s)

    def request(origin: str | None, host: str) -> Any:
        headers = {"host": host} | ({"origin": origin} if origin is not None else {})
        return SimpleNamespace(headers=headers)

    for origin in lan.lan_origins([IP], NAME, 11000):
        host = origin.split("://", 1)[1]
        assert csrf._origin_ok(request(origin, host)) and core._origin_allowed(request(origin, host))
    for origin in ("https://evil.example", f"https://{NAME}:11001", "https://192.168.50.24:11000", "null",
                   f"https://{IP}.evil.example:11000"):
        assert not csrf._origin_ok(request(origin, f"{IP}:11000")), origin
        assert not core._origin_allowed(request(origin, f"{IP}:11000")), origin


def test_lan_mode_passes_the_production_checks(monkeypatch):
    """ENVIRONMENT=production in .env + LAN mode: the production rules run on these settings and accept them (no
    refusal, no warning but the deliberate TRUST_PROXY_HEADERS=false, which LAN mode does not report)."""
    strong = {"ENVIRONMENT": "production", "SECRET_KEY": "pT9-vK2_mQ7xZ4rW8nB5cH3jL6dF1gY0sA7eU4iO2xx",
              "ID_HASH_PEPPER": "Lq4-Wm8_Zt2Kc6Rn9Hy3", "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
              "SUPERADMIN_PASSWORD": "", "AI_PROVIDER_ORDER": "gateway,docker,anthropic"}
    for key, value in strong.items():
        monkeypatch.setenv(key, value)
    env = lan.lan_env(ips=[IP], hostname=NAME, port=11000)
    assert lan.check_settings(env) == []
    s = lan_settings(monkeypatch, **strong)
    assert s.is_production and s.cookie_secure and s.force_https and not s.trust_proxy_headers
    assert [w for w in s.production_warnings() if not w.startswith("TRUST_PROXY_HEADERS=false")] == []
    monkeypatch.setenv("SECRET_KEY", "change-me")  # and a refused production setting stops LAN mode before it listens
    with pytest.raises(lan.LanError, match="SECRET_KEY"):
        lan.check_settings(env)


def test_uvicorn_options_are_tls_only_without_proxy_headers(tmp_path):
    opts = lan.uvicorn_options(port=11000, bind="0.0.0.0", workers=2, cert=tmp_path / "c", key=tmp_path / "k")
    assert opts["ssl_certfile"] == str(tmp_path / "c") and opts["ssl_keyfile"] == str(tmp_path / "k")
    assert opts["ssl_ciphers"] == lan.SECURE_CIPHERS and opts["ssl_version"] == ssl.PROTOCOL_TLS_SERVER
    assert opts["proxy_headers"] is False and opts["forwarded_allow_ips"] == "" and opts["server_header"] is False
    assert opts["reload"] is False and opts["factory"] is True and opts["workers"] == 2 and opts["host"] == "0.0.0.0"
    assert lan.tls_floor_ok()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.set_ciphers(lan.SECURE_CIPHERS)
    tls12 = [c["name"] for c in ctx.get_ciphers() if c["protocol"] == "TLSv1.2"]
    assert tls12 and all(n.startswith("ECDHE-") and ("GCM" in n or "CHACHA20" in n) for n in tls12)


def test_addresses_and_names(monkeypatch):
    assert lan.usable_lan_ip("192.168.1.5") and lan.usable_lan_ip("10.1.2.3") and lan.usable_lan_ip("172.20.10.2")
    assert lan.usable_lan_ip("100.70.1.2")  # CGNAT / hotspots
    for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "::1", "fe80::1", "nonsense", "224.0.0.1"):
        assert not lan.usable_lan_ip(bad), bad

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        answers = {("ipconfig", "getifaddr", "en0"): (0, "192.168.1.5\n"), ("ipconfig", "getifaddr", "en1"): (1, ""),
                   ("scutil", "--get", "LocalHostName"): (0, "Sam's MacBook Air\n")}
        code, out = answers.get(tuple(cmd), (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    ips = lan.detect_lan_ips(run=fake_run, platform="darwin")
    assert ips[0] == "192.168.1.5" and all(lan.usable_lan_ip(ip) for ip in ips)
    assert lan.local_hostname(run=fake_run, platform="darwin") == "sam-s-macbook-air.local"
    assert lan.normalize_name("MyMac.local") == "mymac.local" and lan.normalize_name("My Mac") == "my-mac.local"
    with pytest.raises(lan.LanError):
        lan.normalize_name("...")


def test_the_port_check_sees_a_server_on_loopback_only(monkeypatch, tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert lan.port_in_use("127.0.0.1", port)
        # the development server on 127.0.0.1:<port>: LAN mode on 0.0.0.0:<port> must not start next to it
        assert lan.port_in_use("0.0.0.0", port)
    assert not lan.port_in_use("127.0.0.1", free_port())
    env_file = tmp_path / ".env"
    env_file.write_text("HOST=127.0.0.1\nPORT=11000   # the web app port\n", encoding="utf-8")
    monkeypatch.delenv("PORT", raising=False)
    assert lan._port_from_settings(env_file) == 11000
    monkeypatch.setenv("PORT", "11005")
    assert lan._port_from_settings(env_file) == 11005


def test_a_restart_right_after_stopping_is_not_refused_for_the_old_servers_time_wait_sockets():
    """Ctrl+C after a phone session leaves the stopped server's connections in TIME_WAIT for ~30 s. Nothing listens,
    uvicorn (SO_REUSEADDR) would bind - but the plain-bind check refused: 'already in use (the development server?)'."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    phone = socket.create_connection(("127.0.0.1", port))
    conn, _ = server.accept()
    conn.close()  # the server closes first (as a stopping uvicorn does): its side ends in TIME_WAIT
    time.sleep(0.05)
    phone.close()
    server.close()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
        try:
            plain.bind(("127.0.0.1", port))
        except OSError:
            pass  # the TIME_WAIT leftover is there: exactly the hunter's state
        else:
            pytest.skip("this system left no TIME_WAIT socket to test against")
    assert not lan.port_in_use("127.0.0.1", port)
    assert not lan.port_in_use("0.0.0.0", port, also=[IP])  # IP is not this computer's: never probed
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as dev:  # ... while a real listener still counts
        dev.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        dev.bind(("127.0.0.1", port))
        dev.listen(1)
        assert lan.port_in_use("127.0.0.1", port) and lan.port_in_use("0.0.0.0", port)


async def test_the_hsts_header_suits_a_lan_host():
    sent: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
                                (b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    wrapped = lan.LanHeaders(app)
    for host, expected in ((f"{IP}:11000", b"max-age=31536000"), (f"{NAME}:11000", b"max-age=31536000"),
                           ("localhost:11000", None), ("127.0.0.1:11000", None), ("[::1]:11000", None)):
        sent.clear()
        await wrapped({"type": "http", "headers": [(b"host", host.encode())]}, None, send)
        hsts = [v for k, v in sent[0]["headers"] if k == b"strict-transport-security"]
        assert hsts == ([expected] if expected else []), host
        assert (b"content-type", b"text/plain") in sent[0]["headers"]


def test_phone_steps_and_qr_code(monkeypatch, tmp_path):
    text = lan.phone_steps(tmp_path / "ironpulse-lan-ca.crt", [f"https://{IP}:11000", f"https://{NAME}:11000"])
    for needle in (str(tmp_path / "ironpulse-lan-ca.crt"), "iPhone", "Certificate Trust Settings", "Android",
                   "CA certificate", f"https://{IP}:11000", ".local"):
        assert needle in text
    monkeypatch.setitem(sys.modules, "qrcode", None)  # the package is not installed ...
    monkeypatch.setattr(lan.shutil, "which", lambda name: None)  # ... nor the tool
    assert lan.qr_lines("https://x") is None
    monkeypatch.setattr(lan.shutil, "which", lambda name: "/usr/bin/qrencode" if name == "qrencode" else None)
    fake = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="█▀█\n█▄█\n", stderr="")  # noqa: E731
    assert lan.qr_lines("https://x", run=fake) == ["█▀█", "█▄█"]


def test_dry_run_and_print_ca(tmp_path, capsys):
    assert lan.main(["--dry-run", "--json", "--ip", IP, "--ip", "8.8.8.8", "--name", "TestMac", "--port", "11000",
                     "--tls-dir", str(tmp_path / "tls")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ips"] == [IP] and plan["hostname"] == NAME and plan["env"]["ALLOWED_HOSTS"] == f"{IP},{NAME},localhost"
    assert plan["urls"] == [f"https://{IP}:11000", f"https://{NAME}:11000"]
    assert not (tmp_path / "tls").exists()  # a dry run writes nothing
    assert lan.main(["--print-ca", "--tls-dir", str(tmp_path / "tls")]) == 2
    lan.ensure_certificates(tmp_path / "tls", [NAME, "localhost"], [IP])
    capsys.readouterr()
    assert lan.main(["--print-ca", "--tls-dir", str(tmp_path / "tls")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "tls" / lan.CA_CERT)
    assert lan.main(["--dry-run", "--ip", "8.8.8.8", "--name", "x"]) == 2  # a public address only: refused


# ===================================================================================== a real server
def _https(port: int, ca: Path, *, host: str, sni: str = NAME, method: str = "GET", path: str = "/healthz",
           headers: dict[str, str] | None = None, body: bytes = b"") -> tuple[int, list[tuple[str, str]], bytes]:
    ctx = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection(("127.0.0.1", port), timeout=15) as raw, ctx.wrap_socket(raw, server_hostname=sni) as tls:
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close", "User-Agent: pytest-lan/1.0",
                 f"Content-Length: {len(body)}", *(f"{k}: {v}" for k, v in (headers or {}).items())]
        tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        data = b""
        while chunk := tls.recv(65536):
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    parsed = [(k.strip().lower(), v.strip()) for k, v in (line.split(":", 1) for line in head_lines[1:] if ":" in line)]
    return int(head_lines[0].split()[1]), parsed, rest


def _tls_ok(port: int, ca: Path, *, minimum: Any = None, maximum: Any = None, ciphers: str | None = None) -> str | None:
    ctx = ssl.create_default_context(cafile=str(ca))
    if ciphers:
        ctx.set_ciphers(ciphers)
    if minimum is not None:
        ctx.minimum_version = minimum
    if maximum is not None:
        ctx.maximum_version = maximum
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
                ctx.wrap_socket(raw, server_hostname=NAME) as tls:
            return tls.version()
    except (ssl.SSLError, ConnectionError, OSError):
        return None


@pytest.fixture(scope="module")
def lan_server(tmp_path_factory):
    """The real LAN launcher (scripts/lan.py) in a subprocess on its own port, bound to 127.0.0.1 (no firewall prompt),
    on this package's test database, without Redis."""
    tls = tmp_path_factory.mktemp("lan") / "tls"
    port = free_port()
    env = {**os.environ, "REDIS_URL": "", "RUN_BACKGROUND_JOBS": "false", "ENVIRONMENT": "test",
           "DATABASE_URL": os.environ["DATABASE_URL"], "PYTHONUNBUFFERED": "1"}
    env.pop("PORT", None)
    log = tls.parent / "server.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "lan.py"), "--port", str(port), "--ip", IP,
                                 "--name", "testmac", "--bind", "127.0.0.1", "--tls-dir", str(tls), "--no-qr"],
                                cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                pytest.fail(f"the LAN server stopped: {log.read_text()[-3000:]}")
            if (tls / lan.CA_CERT).exists() and _tls_ok(port, tls / lan.CA_CERT):
                break
            if time.monotonic() > deadline:
                pytest.fail(f"the LAN server did not start: {log.read_text()[-3000:]}")
            time.sleep(0.3)
        yield SimpleNamespace(port=port, ca=tls / lan.CA_CERT, log=log)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_server_answers_only_its_lan_names(lan_server):
    p, ca = lan_server.port, lan_server.ca
    for host, sni, hsts in ((f"{IP}:{p}", IP, "max-age=31536000"), (f"{NAME}:{p}", NAME, "max-age=31536000"),
                            (f"localhost:{p}", "localhost", None)):
        status, headers, body = _https(p, ca, host=host, sni=sni)
        assert status == 200 and json.loads(body)["database"] is True, host
        assert [v for k, v in headers if k == "strict-transport-security"] == ([hsts] if hsts else []), host
        assert ("server", "uvicorn") not in headers
    for host in (f"evil.example:{p}", f"127.0.0.1:{p}", f"{IP}.nip.io:{p}", f"192.168.50.24:{p}"):  # DNS rebinding ...
        status, _, _ = _https(p, ca, host=host)
        assert status == 400, host
    for path in ("/api/docs", "/api/openapi.json", "/docs", "/openapi.json", "/redoc"):
        status, _, _ = _https(p, ca, host=f"{NAME}:{p}", path=path)
        assert status == 404, path
    status, headers, _ = _https(p, ca, host=f"{NAME}:{p}", path="/")
    cookies = [v for k, v in headers if k == "set-cookie"]
    assert cookies and all(c.startswith("__Host-") and "; Secure" in c for c in cookies), cookies


def test_the_server_speaks_modern_tls_only(lan_server):
    p, ca = lan_server.port, lan_server.ca
    assert _tls_ok(p, ca) == "TLSv1.3"
    assert _tls_ok(p, ca, maximum=ssl.TLSVersion.TLSv1_2, ciphers="ECDHE-ECDSA-AES128-GCM-SHA256") == "TLSv1.2"
    assert _tls_ok(p, ca, maximum=ssl.TLSVersion.TLSv1_2, ciphers="ECDHE-ECDSA-CHACHA20-POLY1305") == "TLSv1.2"
    # CBC / SHA-1 suites (no AEAD) are refused, and so are TLS 1.0 and 1.1
    assert _tls_ok(p, ca, maximum=ssl.TLSVersion.TLSv1_2, ciphers="ECDHE-ECDSA-AES128-SHA:@SECLEVEL=0") is None
    assert _tls_ok(p, ca, minimum=ssl.TLSVersion.TLSv1, maximum=ssl.TLSVersion.TLSv1_1,
                   ciphers="DEFAULT:@SECLEVEL=0") is None
    with pytest.raises(ssl.SSLCertVerificationError):  # the certificate names exactly the LAN names
        ctx = ssl.create_default_context(cafile=str(ca))
        with socket.create_connection(("127.0.0.1", p), timeout=10) as raw, \
                ctx.wrap_socket(raw, server_hostname="evil.example"):
            pass


def test_the_origin_checks_accept_only_lan_origins(lan_server):
    p, ca = lan_server.port, lan_server.ca
    form = b"identifier=x&password=y"
    ctype = {"Content-Type": "application/x-www-form-urlencoded"}
    status, _, body = _https(p, ca, host=f"{NAME}:{p}", method="POST", path="/auth/login", body=form,
                             headers={**ctype, "Origin": "https://evil.example"})
    assert status == 403 and b"Cross-site request blocked" in body
    status, _, body = _https(p, ca, host=f"{NAME}:{p}", method="POST", path="/auth/login", body=form,
                             headers={**ctype, "Origin": f"https://{NAME}:{p}"})
    assert status == 403 and b"Cross-site request blocked" not in body  # the origin passed; the CSRF token is missing

    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    def ws_close_code(origin: str) -> int:
        ctx = ssl.create_default_context(cafile=str(ca))
        sock = socket.create_connection(("127.0.0.1", p), timeout=10)
        with connect(f"wss://{NAME}:{p}/ws", sock=sock, ssl=ctx, server_hostname=NAME, origin=origin,
                     proxy=None, open_timeout=10) as ws:
            try:
                ws.recv(timeout=10)
            except ConnectionClosed as exc:
                return exc.rcvd.code if exc.rcvd else -1
        return -1

    assert ws_close_code("https://evil.example") == 4403  # cross-site WebSocket hijacking refused
    assert ws_close_code(f"https://{NAME}:{p}") == 4401  # a LAN origin passes the check (then: not signed in)


# ===================================================================================== run.sh / run.ps1
@pytest.mark.skipif(sys.platform.startswith("win") or shutil.which("bash") is None, reason="bash")
def test_run_sh_lan_mode_and_dry_run():
    def sh(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / "run.sh"), *args], cwd=str(ROOT), capture_output=True, text=True,
                              timeout=60, env={**os.environ, "NO_COLOR": "1"})

    assert subprocess.run(["bash", "-n", str(ROOT / "run.sh")]).returncode == 0
    plan = sh("--lan", "--dry-run", "--no-services", "--no-ai-docker", "--port", "11042")
    assert plan.returncode == 0, plan.stderr
    assert "scripts/lan.py --workers 1 --port 11042" in plan.stdout.replace("\\ ", " ")
    assert "[lan] environment: HOST=0.0.0.0 PORT=11042" in plan.stdout and "COOKIE_SECURE=true" in plan.stdout
    assert "--ssl-certfile" in plan.stdout and "[ai]" not in plan.stdout  # --no-ai-docker
    assert "would run" in sh("--dry-run", "--no-services", "--no-ai-docker").stdout
    assert "-m app" in sh("--dry-run", "--no-services", "--no-ai-docker").stdout
    assert "manage.py run --workers 3" in sh("--dry-run", "--no-services", "--no-ai-docker", "--workers", "3").stdout
    assert "cannot be combined" in sh("--lan", "--reload").stderr
    assert "leave out --host" in sh("--lan", "--host", "0.0.0.0").stderr
    usage = sh("--help").stdout
    assert "--lan" in usage and "--no-ai-docker" in usage and "--dry-run" in usage


def test_run_ps1_has_the_lan_switch():
    text = (ROOT / "run.ps1").read_text(encoding="utf-8")
    for needle in ("[switch]$Lan", "[switch]$NoAiDocker", "[switch]$DryRun", 'Join-Path $Root "scripts\\lan.py"',
                   '"ai-fallback"', '"--dry-run"', "-Lan cannot be combined with -Reload"):
        assert needle in text, needle
