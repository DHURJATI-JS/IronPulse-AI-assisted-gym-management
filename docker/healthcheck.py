"""Container health check of the IronPulse image (docker/Dockerfile HEALTHCHECK): GET /healthz inside the container.

ENVIRONMENT=production turns FORCE_HTTPS on by default, and a plain-HTTP request would get a 308 redirect - so the
probe says it came through HTTPS (``X-Forwarded-Proto: https``). The app (and uvicorn) believe that header only from
TRUSTED_PROXIES - default 127.0.0.1,::1, and the probe connects from 127.0.0.1: keep 127.0.0.1 in TRUSTED_PROXIES. The
probe's Host is the first ALLOWED_HOSTS entry ('*.example.com' -> 'health.example.com'), so the host check accepts it.

Exit status 0 = healthy (HTTP 200: the app and its database answer), 1 = anything else (a 503 when the database is
down, a refused connection while the app starts, a 308 when the probe's X-Forwarded-Proto is not believed).
Standard library only.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 5.0


def probe_host(allowed_hosts: str | None) -> str:
    """The Host header for the probe: the first ALLOWED_HOSTS entry the host check accepts."""
    first = next((h.strip() for h in (allowed_hosts or "").split(",") if h.strip()), "localhost")
    if first == "*":
        return "localhost"
    if first.startswith("*."):
        first = "health" + first[1:]
    if ":" in first and not first.startswith("["):  # an IPv6 literal keeps its brackets in a Host header
        first = f"[{first}]"
    return first


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003 - a 308 is an answer, never followed
        return None


def check(port: str | int, allowed_hosts: str | None, *, timeout: float = TIMEOUT) -> tuple[bool, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={
        "Host": probe_host(allowed_hosts), "X-Forwarded-Proto": "https", "User-Agent": "ironpulse-healthcheck/1.0"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 307, 308):
            return False, (f"HTTP {exc.code} to HTTPS: the probe's X-Forwarded-Proto was not believed - keep 127.0.0.1 "
                           "in TRUSTED_PROXIES")
        if exc.code == 400:
            return False, "HTTP 400: the probe's Host is not accepted - check ALLOWED_HOSTS"
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - refused while starting, timed out ...
        return False, f"{exc.__class__.__name__}: {getattr(exc, 'reason', exc)}"


def main() -> int:
    ok, detail = check(os.environ.get("PORT", "10000"), os.environ.get("ALLOWED_HOSTS"))
    if not ok:
        print(f"healthcheck: {detail}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
