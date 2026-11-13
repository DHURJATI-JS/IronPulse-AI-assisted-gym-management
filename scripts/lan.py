"""Serve IronPulse to your phone and other devices on your local network - HTTPS only (README "Use it on your phone").

    ./run.sh --lan                                           macOS / Linux (port: PORT from .env, 11000)
    powershell -ExecutionPolicy Bypass -File run.ps1 -Lan    Windows
    python scripts/lan.py --dry-run                          show addresses, certificate and settings; start nothing
    python scripts/lan.py --print-ca                         print the path of the CA file to install on phones

What it does:

* finds this computer's LAN address(es) (macOS: ``ipconfig getifaddr en0/en1``) and its Bonjour name
  (``scutil --get LocalHostName`` -> ``<name>.local``);
* keeps a small local certificate authority in var/tls/ (git-ignored; keys mode 0600 in a 0700 folder) and a server
  certificate for exactly those names + ``localhost``, renewed when the LAN address changes or it nears expiry. You
  install the CA certificate (``ironpulse-lan-ca.crt``) on each phone ONCE. The CA is name-constrained to private
  networks, ``*.local`` and ``localhost``: even a stolen CA key cannot impersonate any public website on a phone that
  trusts it. ``--mkcert`` uses mkcert's CA instead (when you already trust it on your phones);
* starts uvicorn on 0.0.0.0:<port> with TLS 1.2+ and forward-secret AEAD ciphers only, and these settings - as
  environment variables, .env is never edited: DEBUG/RELOAD off (no /api/docs), COOKIE_SECURE (``__Host-`` Secure
  cookies), FORCE_HTTPS, TRUST_PROXY_HEADERS=false (devices connect directly: rate limits and bans apply per device
  address), ALLOWED_HOSTS = exactly the LAN address(es), ``<name>.local`` and ``localhost`` (any other Host header is
  answered 400 - DNS rebinding included), PUBLIC_BASE_URL=https://<name>.local:<port>, CORS_ORIGINS empty and an
  empty RATE_LIMIT_WHITELIST. The CSRF and WebSocket origin checks then accept only those origins;
* sends HSTS without includeSubDomains/preload (a LAN host), and none for ``localhost`` - a browser would otherwise
  force HTTPS on every localhost port for a year, the development server on http://localhost:<port> included;
* prints the URLs (and a QR code when the ``qrcode`` package or the ``qrencode`` tool is installed).

ENVIRONMENT=production in .env works too: the production checks (secrets, host names ...) run on these settings.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TLS_DIR = ROOT / "var" / "tls"
CA_KEY = "ironpulse-lan-ca.key"
CA_CERT = "ironpulse-lan-ca.crt"  # PEM - the file to install on phones
CA_DER = "ironpulse-lan-ca.cer"  # the same certificate, DER (some Android versions prefer it)
LEAF_KEY = "lan.key"
LEAF_CERT = "lan.crt"
CA_NAME = "IronPulse LAN CA"
CA_DAYS = 3650  # the CA lives 10 years: phones install it once
LEAF_DAYS = 397  # Apple and browsers reject server certificates valid for longer than 398 / 825 days
RENEW_DAYS = 30  # a server certificate this close to expiry is renewed at the next start
DEFAULT_PORT = 11000
BIND_HOST = "0.0.0.0"
# TLS 1.2 suites: ECDHE (forward secrecy) + AEAD only; TLS 1.3 suites are all strong. Python's PROTOCOL_TLS_SERVER
# context refuses TLS 1.0/1.1 already (checked at start: tls_floor_ok).
SECURE_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!eNULL:!MD5:!DSS"
HSTS_VALUE = "max-age=31536000"  # a LAN host: no includeSubDomains, no preload
# what the CA may ever sign (X.509 name constraints): private / link-local / CGNAT / loopback addresses, *.local, localhost
PERMITTED_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                      "127.0.0.0/8", "fc00::/7", "fe80::/10", "::1/128")
PERMITTED_DNS = ("local", "localhost")
INTERFACES = ("en0", "en1")  # macOS: Wi-Fi / Ethernet

Runner = Callable[..., subprocess.CompletedProcess]


class LanError(Exception):
    """A user-facing problem: printed as one line, exit status 2."""


def say(msg: str = "") -> None:
    print(f"[lan] {msg}" if msg else "", flush=True)


# ============================================================================= this computer on the LAN
def usable_lan_ip(text: str) -> bool:
    """An IPv4 address other devices of a home/office network can reach: private, CGNAT or link-local - never a public
    address (LAN mode is for your own network) and never loopback."""
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return False
    return any(ip in ipaddress.ip_network(net) for net in PERMITTED_NETWORKS)


def _run(run: Runner, *cmd: str) -> str:
    try:
        proc = run(list(cmd), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def detect_lan_ips(*, run: Runner = subprocess.run, platform: str = sys.platform) -> list[str]:
    """This computer's LAN IPv4 address(es): macOS asks en0/en1 (``ipconfig getifaddr``); everywhere the address of
    the default route is added (a UDP socket 'connected' to a documentation address - nothing is sent)."""
    found: list[str] = []
    if platform == "darwin":
        for iface in INTERFACES:
            ip = _run(run, "ipconfig", "getifaddr", iface)
            if ip:
                found.append(ip)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: picks the outgoing interface without sending a packet
            found.append(s.getsockname()[0])
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if usable_lan_ip(ip)]


def _dns_label(text: str) -> str:
    label = re.sub(r"[^a-z0-9-]+", "-", text.strip().lower()).strip("-")
    return label[:63].strip("-")


def local_hostname(*, run: Runner = subprocess.run, platform: str = sys.platform) -> str:
    """``<name>.local`` - the Bonjour (mDNS) name phones on the same network resolve (macOS: LocalHostName)."""
    name = _run(run, "scutil", "--get", "LocalHostName") if platform == "darwin" else ""
    if not name:
        name = socket.gethostname().split(".")[0]
    return f"{_dns_label(name) or 'ironpulse'}.local"


def normalize_name(text: str) -> str:
    """``--name MyMac`` or ``MyMac.local`` -> ``mymac.local``."""
    base = text.strip().lower()
    base = base[: -len(".local")] if base.endswith(".local") else base
    label = _dns_label(base)
    if not label:
        raise LanError(f"'{text}' is not a usable host name (letters, digits and '-')")
    return f"{label}.local"


# ============================================================================= certificates
@dataclass
class CertState:
    ca_cert: Path
    ca_der: Path
    leaf_cert: Path
    leaf_key: Path
    ca_created: bool = False
    leaf_created: bool = False
    reason: str = ""  # why the server certificate was (re)made
    names: list[str] = field(default_factory=list)
    ips: list[str] = field(default_factory=list)
    expires: dt.datetime | None = None
    tool: str = "built-in"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_private(path: Path, data: bytes) -> None:
    """Write a key: created 0600 (never readable by others, not even for a moment), then moved into place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _write_public(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    for key in (CA_KEY, LEAF_KEY):  # a key copied in by hand keeps 0600 too
        if (path / key).exists():
            os.chmod(path / key, 0o600)


def _load(cert_path: Path, key_path: Path) -> tuple[Any, Any] | None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        return None  # the key does not belong to the certificate
    return cert, key


def _new_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())  # P-256: every phone browser, small and fast


def _pem_key(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _make_ca(now: dt.datetime) -> tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{CA_NAME} ({socket.gethostname()[:40]})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "IronPulse (local network only)")])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    constraints = x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(d) for d in PERMITTED_DNS]
        + [x509.IPAddress(ipaddress.ip_network(n)) for n in PERMITTED_NETWORKS],
        excluded_subtrees=None,
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ski, critical=False)
        # non-critical so an old verifier that does not know the extension still accepts the CA; every current phone
        # browser enforces it
        .add_extension(constraints, critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _make_leaf(ca_cert: Any, ca_key: Any, names: list[str], ips: list[str], now: dt.datetime) -> tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = _new_key()
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                     content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def cert_names(cert: Any) -> tuple[list[str], list[str]]:
    """(DNS names, IP addresses) of a certificate's subjectAltName."""
    from cryptography import x509

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    return (sorted(san.get_values_for_type(x509.DNSName)),
            sorted(str(ip) for ip in san.get_values_for_type(x509.IPAddress)))


def _signed_by(cert: Any, ca_cert: Any) -> bool:
    try:
        cert.verify_directly_issued_by(ca_cert)
    except Exception:  # noqa: BLE001 - wrong issuer, bad signature, unsupported key
        return False
    return True


def ensure_certificates(tls_dir: Path, names: list[str], ips: list[str], *, now: dt.datetime | None = None,
                        force: bool = False) -> CertState:
    """The CA (made once) and a server certificate for exactly ``names`` + ``ips``, signed by it. The server
    certificate is reused while it matches; it is made again when the names or LAN addresses changed, it expires
    within RENEW_DAYS, its key is missing or it was not signed by the current CA (``force``: always)."""
    from cryptography.hazmat.primitives import serialization

    now = now or _now()
    _secure_dir(tls_dir)
    state = CertState(ca_cert=tls_dir / CA_CERT, ca_der=tls_dir / CA_DER, leaf_cert=tls_dir / LEAF_CERT,
                      leaf_key=tls_dir / LEAF_KEY, names=list(names), ips=list(ips))
    ca = _load(tls_dir / CA_CERT, tls_dir / CA_KEY)
    if ca is None or ca[0].not_valid_after_utc - now < dt.timedelta(days=RENEW_DAYS):
        ca = _make_ca(now)
        _write_private(tls_dir / CA_KEY, _pem_key(ca[1]))
        _write_public(tls_dir / CA_CERT, ca[0].public_bytes(serialization.Encoding.PEM))
        _write_public(tls_dir / CA_DER, ca[0].public_bytes(serialization.Encoding.DER))
        state.ca_created = True
    elif not (tls_dir / CA_DER).exists():
        _write_public(tls_dir / CA_DER, ca[0].public_bytes(serialization.Encoding.DER))
    ca_cert, ca_key = ca

    want = (sorted(names), sorted(str(ipaddress.ip_address(i)) for i in ips))
    leaf = _load(tls_dir / LEAF_CERT, tls_dir / LEAF_KEY)
    if force:
        state.reason = "renewed on request"
    elif leaf is None:
        state.reason = "new"
    elif not _signed_by(leaf[0], ca_cert):
        state.reason = "the CA changed"
    elif cert_names(leaf[0]) != want:
        state.reason = "the LAN address or name changed"
    elif leaf[0].not_valid_after_utc - now < dt.timedelta(days=RENEW_DAYS):
        state.reason = "it was about to expire"
    if state.reason:
        cert, key = _make_leaf(ca_cert, ca_key, list(names), list(ips), now)
        _write_private(tls_dir / LEAF_KEY, _pem_key(key))
        _write_public(tls_dir / LEAF_CERT, cert.public_bytes(serialization.Encoding.PEM))
        state.leaf_created = True
        state.expires = cert.not_valid_after_utc
    else:
        state.expires = leaf[0].not_valid_after_utc
    return state


def mkcert_certificates(tls_dir: Path, names: list[str], ips: list[str], *, run: Runner = subprocess.run) -> CertState:
    """``--mkcert``: the server certificate from mkcert's CA (the one ``mkcert -install`` trusts; install its
    rootCA.pem on the phones). Nothing is installed into any trust store here."""
    exe = shutil.which("mkcert")
    if exe is None:
        raise LanError("--mkcert: mkcert is not installed (macOS: brew install mkcert) - or leave --mkcert out to use "
                       "the built-in CA")
    _secure_dir(tls_dir)
    cmd = [exe, "-ecdsa", "-cert-file", str(tls_dir / LEAF_CERT), "-key-file", str(tls_dir / LEAF_KEY), *names, *ips]
    proc = run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise LanError(f"mkcert failed: {(proc.stderr or proc.stdout or '').strip()[:300]}")
    os.chmod(tls_dir / LEAF_KEY, 0o600)
    caroot = _run(run, exe, "-CAROOT")
    ca = Path(caroot) / "rootCA.pem" if caroot else tls_dir / "rootCA.pem"
    return CertState(ca_cert=ca, ca_der=ca, leaf_cert=tls_dir / LEAF_CERT, leaf_key=tls_dir / LEAF_KEY,
                     leaf_created=True, reason="mkcert", names=list(names), ips=list(ips), tool="mkcert")


# ============================================================================= settings for the server
def lan_hosts(ips: Iterable[str], hostname: str) -> list[str]:
    """ALLOWED_HOSTS: exactly the LAN address(es), the Bonjour name and localhost."""
    return list(dict.fromkeys([*ips, hostname, "localhost"]))


def lan_origins(ips: Iterable[str], hostname: str, port: int) -> list[str]:
    """The browser origins of LAN mode (what the CSRF and WebSocket origin checks accept)."""
    return [f"https://{h}:{port}" for h in lan_hosts(ips, hostname)]


def lan_env(*, ips: list[str], hostname: str, port: int, workers: int = 1, bind: str = BIND_HOST) -> dict[str, str]:
    """The environment the LAN server runs with - environment variables only, .env is never edited (they win over it)."""
    env = {
        "HOST": bind,
        "PORT": str(port),
        "DEBUG": "false",  # no /api/docs, /api/openapi.json or debug pages
        "RELOAD": "false",
        "COOKIE_SECURE": "true",  # __Host- cookies, Secure
        "FORCE_HTTPS": "true",  # HSTS + upgrade-insecure-requests (there is no plain-HTTP listener at all)
        "TRUST_PROXY_HEADERS": "false",  # devices connect directly: X-Forwarded-For is never believed
        "ALLOWED_HOSTS": ",".join(lan_hosts(ips, hostname)),
        "PUBLIC_BASE_URL": f"https://{hostname}:{port}",
        "CORS_ORIGINS": "",  # same-origin only
        "RATE_LIMIT_WHITELIST": "",  # every device is rate-limited (and can be banned) by its own address
        "IRONPULSE_LAN": "1",
        "IRONPULSE_WORKERS": str(workers),  # app.db's connection-budget check
    }
    if workers > 1:
        env["REALTIME_PG_BRIDGE"] = "true"  # live events fan out between the worker processes
    return env


def tls_floor_ok() -> bool:
    """Does a server context refuse TLS 1.0/1.1? (Python 3.10+ sets TLS 1.2 as the minimum of PROTOCOL_TLS_SERVER.)"""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).minimum_version >= ssl.TLSVersion.TLSv1_2


def uvicorn_options(*, port: int, bind: str, workers: int, cert: Path, key: Path) -> dict[str, Any]:
    """Keyword arguments for ``uvicorn.run`` (the app is ``create_app``, a factory)."""
    return {
        "host": bind, "port": port, "workers": workers, "factory": True, "app_dir": str(ROOT),
        "ssl_certfile": str(cert), "ssl_keyfile": str(key), "ssl_ciphers": SECURE_CIPHERS,
        "ssl_version": ssl.PROTOCOL_TLS_SERVER,
        "proxy_headers": False, "forwarded_allow_ips": "", "server_header": False, "reload": False,
        "log_level": "info",
    }


def _address_taken(host: str, port: int) -> bool:
    """Is ``host:port`` held by a live socket - a bind with SO_REUSEADDR, as uvicorn binds, refused with 'address in
    use'? A TIME_WAIT leftover never refuses it; an address this computer does not have (another device's, or ::1 with
    IPv6 off) is no conflict - nothing is ever sent anywhere."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    with s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def port_in_use(bind: str, port: int, also: Iterable[str] = ()) -> bool:
    """Is ``port`` taken by a live server - on ``bind``, on 127.0.0.1 / ::1 (the development server on the same port:
    on macOS a 0.0.0.0 listener could start next to it and send localhost to the wrong server), on the wildcard
    addresses or on one of the LAN addresses ``also``? Each address is bound once with SO_REUSEADDR, exactly as uvicorn
    will: the TIME_WAIT sockets a stopped server leaves behind for ~30 s do not count - a plain bind failed on them, so a
    restart right after Ctrl+C was refused as 'already in use (the development server?)'."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind, port))
        except OSError:
            return True
    others = dict.fromkeys(["127.0.0.1", "::1", "0.0.0.0", "::", *also])
    return any(_address_taken(host, port) for host in others if host != bind)


# ============================================================================= the ASGI wrapper
def _host_name(host_header: str) -> str:
    """The name part of a Host header: ``name:port`` -> name, ``[::1]:port`` -> ::1."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    return host.split(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback_host(host_header: str) -> bool:
    name = _host_name(host_header)
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class LanHeaders:
    """Wraps the app in LAN mode: the HSTS header loses includeSubDomains/preload (``HSTS_VALUE``), and requests for
    localhost get none - a browser keeps HSTS per host name for every port, so it would turn the development server
    on http://localhost into an error page for a year."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"host"), "")
        loopback = _is_loopback_host(host)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers") or [] if k.lower() != b"strict-transport-security"]
                if not loopback:
                    headers.append((b"strict-transport-security", HSTS_VALUE.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app() -> Any:
    """uvicorn factory (``scripts.lan:create_app``): the IronPulse app, built from the LAN environment, wrapped."""
    from app.main import app

    return LanHeaders(app)


# ============================================================================= the console
def phone_steps(ca: Path, urls: list[str]) -> str:
    return "\n".join([
        f"Install the CA certificate on each phone ONCE: {ca}",
        "  (AirDrop or e-mail it to the phone; it only works for this network's addresses and *.local names)",
        "  iPhone:  open the file -> Settings > Profile Downloaded > Install;",
        "           then Settings > General > About > Certificate Trust Settings > turn on full trust for "
        f"'{CA_NAME}'",
        "  Android: Settings > Security > More security settings > Encryption & credentials > Install a certificate",
        "           > CA certificate > pick the file (Chrome trusts it; Firefox: enable 'Use third party CA "
        "certificates')",
        "Then open on the phone (same Wi-Fi): " + "  or  ".join(urls),
        "  (Android often cannot resolve .local names - use the address with the IP there)",
    ])


def qr_lines(url: str, *, run: Runner = subprocess.run) -> list[str] | None:
    """A terminal QR code for ``url``: the ``qrcode`` package or the ``qrencode`` tool, when installed."""
    try:
        import io

        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        out = io.StringIO()
        qr.print_ascii(out=out, invert=True)
        return out.getvalue().rstrip("\n").splitlines()
    except Exception:  # noqa: BLE001 - not installed (or an old version): try the CLI
        pass
    exe = shutil.which("qrencode")
    if exe is None:
        return None
    text = _run(run, exe, "-t", "UTF8", "-m", "1", url)
    return text.splitlines() if text else None


# ============================================================================= plan + main
@dataclass
class Plan:
    ips: list[str]
    hostname: str
    port: int
    bind: str
    workers: int
    env: dict[str, str]
    certs: CertState
    urls: list[str]
    options: dict[str, Any]
    skipped_ips: list[str] = field(default_factory=list)


def _port_from_settings(env_file: Path | None = None) -> int:
    """PORT from the environment, else from .env (the setting the app itself reads), else DEFAULT_PORT."""
    raw = os.environ.get("PORT", "").strip()
    path = env_file or ROOT / ".env"
    if not raw and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "PORT":
                raw = value.split("#", 1)[0].strip().strip("'\"")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError as exc:
        raise LanError(f"PORT={raw!r} is not a number - fix PORT in .env or pass --port") from exc


def make_plan(args: argparse.Namespace, *, run: Runner = subprocess.run) -> Plan:
    port = args.port or _port_from_settings()
    if not 1 <= port <= 65535:
        raise LanError(f"port {port} is out of range")
    requested = list(args.ip or []) or detect_lan_ips(run=run)
    ips = [ip for ip in dict.fromkeys(requested) if usable_lan_ip(ip)]
    skipped = [ip for ip in requested if ip not in ips]
    if not ips:
        raise LanError("no LAN address found - connect this computer to your Wi-Fi/network first (or pass --ip "
                       "192.168.x.y); public addresses are never used")
    hostname = normalize_name(args.name) if args.name else local_hostname(run=run)
    workers = max(1, int(args.workers or 1))
    tls_dir = Path(args.tls_dir) if args.tls_dir else TLS_DIR
    names = [hostname, "localhost"]
    if args.dry_run:
        certs = CertState(ca_cert=tls_dir / CA_CERT, ca_der=tls_dir / CA_DER, leaf_cert=tls_dir / LEAF_CERT,
                          leaf_key=tls_dir / LEAF_KEY, names=names, ips=ips, reason="(dry run: not checked)")
    elif args.mkcert:
        certs = mkcert_certificates(tls_dir, names, ips, run=run)
    else:
        certs = ensure_certificates(tls_dir, names, ips, force=args.renew)
    env = lan_env(ips=ips, hostname=hostname, port=port, workers=workers, bind=args.bind)
    urls = [f"https://{ip}:{port}" for ip in ips] + [f"https://{hostname}:{port}"]
    options = uvicorn_options(port=port, bind=args.bind, workers=workers, cert=certs.leaf_cert, key=certs.leaf_key)
    return Plan(ips=ips, hostname=hostname, port=port, bind=args.bind, workers=workers, env=env, certs=certs,
                urls=urls, options=options, skipped_ips=skipped)


def check_settings(env: dict[str, str]) -> list[str]:
    """Load the app settings the way the server will (this environment + .env): the production checks run here, so a
    refused configuration stops before anything listens. Returns the startup warnings."""
    from app.config import ConfigError, build_settings, startup_warnings

    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        settings = build_settings()
    except ConfigError as exc:
        raise LanError("; ".join(exc.problems)) from None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    # TRUST_PROXY_HEADERS=false is the point of LAN mode (devices connect directly): not worth a warning here
    return [w for w in startup_warnings(settings) if not w.startswith("TRUST_PROXY_HEADERS=false")]


def describe(plan: Plan) -> list[str]:
    c = plan.certs
    lines = [f"LAN address(es): {', '.join(plan.ips)}   name: {plan.hostname}   port: {plan.port}"
             + (f"   (ignored, not a private address: {', '.join(plan.skipped_ips)})" if plan.skipped_ips else "")]
    if c.tool == "mkcert":
        lines.append(f"certificate: {c.leaf_cert} (mkcert); CA to install on phones: {c.ca_cert}")
    elif c.reason.startswith("(dry run"):
        lines.append(f"certificates in {c.leaf_cert.parent}: not checked (dry run); CA for phones: {c.ca_cert}")
    else:
        made = "new CA - install it on your phones" if c.ca_created else "CA unchanged"
        leaf = f"server certificate {'made: ' + c.reason if c.leaf_created else 'reused'}"
        until = f", valid until {c.expires:%Y-%m-%d}" if c.expires else ""
        lines.append(f"certificates in {c.leaf_cert.parent}: {made}; {leaf}{until}")
    lines.append("HTTPS only (TLS 1.2+, forward-secret AEAD ciphers); allowed hosts: " + plan.env["ALLOWED_HOSTS"])
    return lines


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lan.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, help=f"port (default: PORT from .env, else {DEFAULT_PORT})")
    ap.add_argument("--workers", type=int, default=1, metavar="N", help="uvicorn worker processes")
    ap.add_argument("--ip", action="append", metavar="ADDR", help="LAN address to serve (repeatable; default: detected)")
    ap.add_argument("--name", help="Bonjour name (default: this Mac's LocalHostName) -> <name>.local")
    ap.add_argument("--bind", default=BIND_HOST, help=argparse.SUPPRESS)  # tests bind 127.0.0.1
    ap.add_argument("--tls-dir", help=argparse.SUPPRESS)  # tests use a temporary folder
    ap.add_argument("--renew", action="store_true", help="make a new server certificate now")
    ap.add_argument("--mkcert", action="store_true", help="use mkcert's CA instead of the built-in one")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen (as JSON with --json); start nothing")
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--print-ca", action="store_true", help="print the path of the CA certificate and exit")
    ap.add_argument("--no-qr", action="store_true", help="do not print a QR code")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.print_ca:
            tls_dir = Path(args.tls_dir) if args.tls_dir else TLS_DIR
            ca = tls_dir / CA_CERT
            if not ca.exists():
                raise LanError(f"no CA yet - start LAN mode once (./run.sh --lan) to create {ca}")
            print(ca)
            return 0
        plan = make_plan(args)
        if args.dry_run:
            if args.json:
                print(json.dumps({"ips": plan.ips, "hostname": plan.hostname, "port": plan.port, "env": plan.env,
                                  "urls": plan.urls, "uvicorn": {k: str(v) for k, v in plan.options.items()}}, indent=2))
                return 0
            for line in describe(plan):
                say(line)
            say("environment: " + " ".join(f"{k}={v}" for k, v in plan.env.items()))
            say("would run: uvicorn scripts.lan:create_app --factory --host {bind} --port {port} --ssl-certfile ... "
                "--ssl-keyfile ... --ssl-ciphers '{ciphers}' --no-proxy-headers --no-server-header{workers}".format(
                    bind=plan.bind, port=plan.port, ciphers=SECURE_CIPHERS,
                    workers=f" --workers {plan.workers}" if plan.workers > 1 else ""))
            for url in plan.urls:
                say(f"open: {url}")
            return 0
        if not tls_floor_ok():
            raise LanError("this Python's TLS library still accepts TLS 1.0/1.1 - upgrade Python (3.10+) / OpenSSL")
        if port_in_use(plan.bind, plan.port, also=plan.ips):
            raise LanError(f"port {plan.port} is already in use on this computer (the development server?) - stop it, "
                           f"or pick another port: ./run.sh --lan --port {plan.port + 1}")
        for line in check_settings(plan.env):
            say(f"warning: {line}")
        for line in describe(plan):
            say(line)
        say()
        for line in phone_steps(plan.certs.ca_cert, plan.urls).splitlines():
            say(line)
        if not args.no_qr:
            qr = qr_lines(plan.urls[0])
            if qr:
                say()
                print("\n".join(qr), flush=True)
            else:
                say("(pip install qrcode, or brew install qrencode, for a QR code here)")
        say()
        if sys.platform == "darwin":
            say("macOS will ask once whether Python may accept incoming network connections - choose Allow.")
        say(f"serving on https://{plan.bind}:{plan.port} - Ctrl+C to stop")
        os.environ.update(plan.env)
        import uvicorn

        uvicorn.run("scripts.lan:create_app", **plan.options)
        return 0
    except LanError as exc:
        print(f"[lan] error: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
