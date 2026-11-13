"""One simulated browser behind a TLS-terminating reverse proxy.

The load generator plays the proxy: every request carries the user's own ``X-Forwarded-For`` address (RFC 2544
benchmarking range) and ``X-Forwarded-Proto: https``, so the server (TRUST_PROXY_HEADERS=true, 127.0.0.1 trusted)
sees one client per simulated user and applies its per-IP rate limits, bans and AI guard as in production.

Cookies are handled HERE, not by the HTTP client: in production mode the app sets ``__Host-ip_session`` /
``__Host-ip_csrf`` with the ``Secure`` attribute, which a cookie jar must not send over the plain-http hop to the
server. The jar below is the browser's view (the browser talks https to the proxy), so we send the ``Cookie`` header
ourselves. Redirects are followed manually like a browser: every hop is a request of its own (with its own stats
entry), Set-Cookie of each hop is applied before the next one, and a redirect to the sign-in page while signed in is
reported as a failure - as is a page view that ends on a DIFFERENT page than the one requested (e.g. /me -> /identity)
unless the task allows that target.

Browser cache: static assets and images are fetched once, then - like a browser without Cache-Control from the server
(StaticFiles sends only ETag / Last-Modified) - kept fresh for 10 % of their age (heuristic freshness, RFC 9111
4.2.2) and revalidated with If-None-Match / If-Modified-Since after that (expect 304).

Double-submit protection (app/core/submit_guard.py): a plain form POST carries the hidden one-time ``submit_id`` the
page rendered into that form (the csrf() macro), taken from the page the form came from - a fresh id per render, so a
fresh one per submission; IP.api-style calls (JSON / FormData) send ``X-Submit-Id`` with a new id per call, as
00-core.js does.

One session per account (SINGLE_SESSION_PER_ACCOUNT): "signed in elsewhere" on a user's own session - a 401 with
``X-Signed-Out: elsewhere``, a page view sent to the sign-in page that then says "signed in on another device", or a
socket closed with 4409 - is a FAILURE: two simulated users share one account (a kit bug).

Every HTML page is also checked for the seeded junk's markup arriving unescaped (``<script>alert(1)</script>`` ...).
"""

from __future__ import annotations

import html as htmllib
import json
import re
import secrets
import time
import uuid
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import urlencode, urlsplit

from ltlib.junk import XSS_MARKERS

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]*)"')
ASSET_RE = re.compile(r'(?:href|src)="(/static/[^"]+)"')
MEDIA_RE = re.compile(r'src="(/media/public/[^"]+)"')
ERROR_PAGE = re.compile(r"<title>(Page not found|Something went wrong|Access denied|Temporarily unavailable|Slow down"
                        r"|Taking too long|Request stopped)", re.I)
FORM_BLOCK = re.compile(r'<form\b([^>]*)>(.*?)</form>', re.S | re.I)
FORM_ACTION = re.compile(r'\baction="([^"]*)"', re.I)
SUBMIT_ID = re.compile(r'name="submit_id"[^>]*?value="([A-Za-z0-9_-]{16,64})"')
SIGNED_OUT_TEXT = "signed in on another device"  # the sign-in page after a sign-in elsewhere (app/security/sessions.py)
ELSEWHERE = "signed in elsewhere - two simulated users share one account (one session per account)"
HANDOFF_COOKIES = ("__Host-ip_rmb", "ip_rmb")  # remembered-account reference handed to the page's script after sign-in
MAX_HOPS = 6
MEDIA_PER_PAGE = 6  # images a browser fetches above the fold (lazy loading does the rest later)
HEURISTIC_FRESHNESS = 0.10  # share of (Date - Last-Modified) a browser keeps a response without Cache-Control fresh

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
]
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"


class _NoCookies(DefaultCookiePolicy):
    """Switches the HTTP client's own cookie jar off - the Browser manages cookies itself."""

    def set_ok(self, cookie: Any, request: Any) -> bool:  # noqa: ARG002
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:  # noqa: ARG002
        return False


def multipart(fields: dict[str, Any]) -> tuple[bytes, str]:
    """multipart/form-data body (what ``new FormData(form)`` sends from the shop's JS components)."""
    boundary = "----LocustFormBoundary" + uuid.uuid4().hex[:16]
    out = []
    for key, value in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n")
    out.append(f"--{boundary}--\r\n")
    return "".join(out).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


class _FormParser(HTMLParser):
    """The inputs of one <form> as a browser would submit them (hidden + text values, the checked radio of each group
    or the first enabled one, the selected <option> or the first non-empty one; checkboxes listed separately)."""

    def __init__(self, form_id: str | None, action: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id, self.action_want = form_id, action
        self.inside = False
        self.done = False
        self.action = ""
        self.fields: dict[str, str] = {}
        self.radios: dict[str, list[tuple[str, bool, bool]]] = {}  # name -> [(value, checked, disabled)]
        self.checkboxes: dict[str, str] = {}
        self.select: str | None = None
        self.options: dict[str, list[tuple[str, bool]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form" and not self.inside and not self.done:
            if (self.form_id and a.get("id") == self.form_id) or (self.action_want and a.get("action", "").startswith(self.action_want)) \
                    or (not self.form_id and not self.action_want):
                self.inside, self.action = True, a.get("action", "")
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text").lower()
            if kind == "radio":
                self.radios.setdefault(name, []).append((a.get("value", ""), "checked" in a, "disabled" in a))
            elif kind == "checkbox":
                self.checkboxes[name] = a.get("value", "on")
            elif kind not in ("submit", "button", "file", "image", "reset"):
                if "disabled" not in a:
                    self.fields.setdefault(name, a.get("value", ""))
        elif tag == "select" and name:
            self.select = None if "disabled" in a else name
            if self.select:
                self.options.setdefault(name, [])
        elif tag == "option" and self.select:
            self.options[self.select].append((a.get("value", ""), "selected" in a))
        elif tag == "textarea" and name:
            self.fields.setdefault(name, "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.select = None
        elif tag == "form" and self.inside:
            self.inside, self.done = False, True

    def result(self) -> tuple[str, dict[str, str], dict[str, str]]:
        out = dict(self.fields)
        for name, opts in self.radios.items():
            pick = next((v for v, checked, dis in opts if checked and not dis), None) or \
                next((v for v, _c, dis in opts if not dis), None)
            if pick is not None:
                out[name] = pick
        for name, opts in self.options.items():
            pick = next((v for v, sel in opts if sel and v), None) or next((v for v, _s in opts if v), None)
            if pick is not None:
                out[name] = pick
        return self.action, out, dict(self.checkboxes)


def parse_form(html: str, *, form_id: str | None = None, action: str | None = None) -> tuple[str, dict[str, str], dict[str, str]]:
    """(action, fields, checkboxes) of the first matching <form> in ``html``."""
    p = _FormParser(form_id, action)
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup: whatever was parsed
        pass
    return p.result()


def sse_events(text: str) -> list[dict[str, Any]]:
    """The JSON payloads of a Server-Sent Events body (``data: {...}`` lines)."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
    return out


class Browser:
    def __init__(self, user: Any, ip: str, ua: str, *, expect_429: bool = False) -> None:
        self.user = user
        self.client = user.client
        self.ip = ip
        self.ua = ua
        self.host = urlsplit(user.host).netloc
        self.origin = f"https://{self.host}"  # what the browser sees: the site is served over https by the proxy
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        self.page_id = uuid.uuid4().hex
        self.cache: set[str] = set()  # map tiles already fetched (browser cache, never revalidated in a session)
        self.assets: dict[str, dict[str, Any]] = {}  # static assets / images: validators + freshness
        self.signed_in = False
        self.unread_at = 0.0
        self.last_url = "/"
        self.expect_429 = expect_429
        self.form_html = ""  # the page a form is about to be submitted from (its forms' submit ids)
        self.device_id = secrets.token_urlsafe(24)  # localStorage "ip.accounts".device (02-accounts.js)
        self.remembered_ref = ""  # the reference the server handed over after a sign-in with "remember"
        self.elsewhere = False  # this browser was signed out by a sign-in elsewhere (reported once per request)
        jar = getattr(self.client, "cookiejar", None)
        if jar is not None:
            jar.set_policy(_NoCookies())

    @staticmethod
    def new_submit_id() -> str:
        """IP.newSubmitId(): 32 random hex characters."""
        return secrets.token_hex(16)

    def take_handoff(self) -> None:
        """02-accounts.js takeHandoff(): the remembered-account reference moves into localStorage, the cookie goes."""
        for name in HANDOFF_COOKIES:
            ref = self.cookies.pop(name, None)
            if ref:
                self.remembered_ref = ref

    def submit_id_for(self, url: str, fields: dict[str, Any]) -> str:
        """The hidden submit_id of the form on the current page that posts to ``url`` (and carries the same hidden
        identifying values, e.g. one cart line's product_id) - else a fresh one, like a submission made by a script."""
        path = self._path(url).split("?", 1)[0]
        for m in FORM_BLOCK.finditer(self.form_html or ""):
            a = FORM_ACTION.search(m.group(1))
            if a is None or self._path(htmllib.unescape(a.group(1))).split("?", 1)[0] != path:
                continue
            body = m.group(2)
            if not all(re.search(rf'name="{re.escape(k)}"[^>]*?value="{re.escape(str(v))}"', body)
                       for k, v in fields.items() if k in ("product_id", "id", "item_id") and f'name="{k}"' in body):
                continue
            sid = SUBMIT_ID.search(body)
            if sid:
                return sid.group(1)
        return self.new_submit_id()

    # ------------------------------------------------------------------------------------------------ cookies
    def _store_cookies(self, resp: Any) -> None:
        headers = getattr(resp, "headers", None)
        if not headers:
            return
        raw = headers.getlist("set-cookie") if hasattr(headers, "getlist") else headers.get_all("set-cookie") or []
        for line in raw:
            parts = [p.strip() for p in line.split(";")]
            if not parts or "=" not in parts[0]:
                continue
            name, value = parts[0].split("=", 1)
            expired = False
            for attr in parts[1:]:
                key, _, val = attr.partition("=")
                key = key.strip().lower()
                if key == "max-age":
                    try:
                        expired = int(val) <= 0
                    except ValueError:
                        pass
                elif key == "expires":
                    try:
                        expired = expired or parsedate_to_datetime(val).timestamp() < time.time()
                    except (TypeError, ValueError):
                        pass
            if expired or value in ('""', ""):
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    # ------------------------------------------------------------------------------------------------ headers
    def headers(self, *, accept: str = HTML_ACCEPT, extra: dict[str, str] | None = None, referer: bool = True) -> dict[str, str]:
        h = {"User-Agent": self.ua, "X-Forwarded-For": self.ip, "X-Forwarded-Proto": "https", "Accept": accept,
             "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8"}
        if self.cookies:
            h["Cookie"] = self.cookie_header()
        if referer and self.last_url:
            h["Referer"] = self.origin + self.last_url
        if extra:
            h.update(extra)
        return h

    def api_headers(self, *, unsafe: bool = False, json_body: bool = False) -> dict[str, str]:
        """What IP.api sends: Accept JSON, X-Page-Id, and X-CSRF-Token + Origin + a new X-Submit-Id (one per call:
        the double-submit guard runs it once) on unsafe methods."""
        extra = {"X-Page-Id": self.page_id}
        if unsafe:
            extra["X-CSRF-Token"] = self.csrf
            extra["Origin"] = self.origin
            extra["X-Submit-Id"] = self.new_submit_id()
        if json_body:
            extra["Content-Type"] = "application/json"
        return self.headers(accept="application/json", extra=extra)

    # ------------------------------------------------------------------------------------------------ requests
    def request(self, method: str, url: str, *, name: str, expect: tuple[int, ...] = (200,), body: Any = None,
                headers: dict[str, str] | None = None, follow: bool = True, html: bool = True,
                allow_login: bool = False, check: Any = None, allow_redirect_to: tuple[str, ...] | None = None) -> Any:
        """Send ``method url`` and follow redirects (GET) like a browser. Returns the final response (or None when the
        transport failed). ``expect``: acceptable statuses of the FINAL response; 3xx hops are fine while followed.
        A GET page view (``html``) that ends on another route than requested is a failure unless the target starts
        with one of ``allow_redirect_to``. ``check(resp)`` returns None (ok), a failure reason, or (reason, note)."""
        hops = 0
        cur_method, cur_url, cur_body, cur_name = method, url, body, name
        cur_headers = headers or self.headers()
        first_route = route_name(url)
        while True:
            with self.client.request(cur_method, cur_url, name=cur_name, data=cur_body, headers=cur_headers,
                                     catch_response=True, allow_redirects=False) as resp:
                self._store_cookies(resp)
                status = resp.status_code or 0
                if status == 0:  # no HTTP answer at all (FastHttp also tags every 4xx/5xx with LocustBadStatusCode)
                    resp.failure(f"transport error: {getattr(resp, 'error', None)!r}")
                    return None
                location = resp.headers.get("location") if resp.headers else None
                if 300 <= status < 400 and follow and location:
                    target = self._path(location)
                    if target.startswith("/auth/login") and self.signed_in and not allow_login:
                        why = self._diagnose_sign_out(target)
                        resp.failure(why or f"unexpected login redirect ({status} -> {target[:80]})")
                        return resp
                    hops += 1
                    if hops > MAX_HOPS:
                        resp.failure("too many redirects")
                        return resp
                    resp.success()
                    cur_method, cur_url, cur_body = "GET", target, None
                    cur_name = f"{name} -> {route_name(target)}"
                    cur_headers = self.headers(accept=HTML_ACCEPT if html else "application/json")
                    continue
                ok, why, note = self._judge(resp, status, expect, html, check)
                if ok and hops and method == "GET" and html and status == 200 and route_name(cur_url) != first_route \
                        and not any(cur_url.startswith(p) for p in (allow_redirect_to or ())):
                    ok, why = False, f"page view redirected to another page ({cur_url[:80]})"
                if note:
                    ctx = resp.request_meta.get("context")
                    if isinstance(ctx, dict):
                        ctx["note"] = note
                if ok:
                    resp.success()
                else:
                    resp.failure(why)
                if html and status == 200 and resp.text:
                    self._after_html(resp.text, cur_url)
                return resp

    def _diagnose_sign_out(self, target: str) -> str | None:
        """A signed-in page view was sent to the sign-in page: follow it once, like the browser, and say why when the
        page tells - "signed in on another device" means another simulated user signed in with this account."""
        try:
            with self.client.request("GET", target, name="GET /auth/login (unexpected sign-out: diagnosis)",
                                     headers=self.headers(), catch_response=True, allow_redirects=False) as resp:
                self._store_cookies(resp)
                text = resp.text or ""
                resp.success()  # the failure is reported on the redirect itself
        except Exception:  # noqa: BLE001 - the diagnosis is best effort
            return None
        if SIGNED_OUT_TEXT in text:
            self.elsewhere = True
            return f"{ELSEWHERE} (sent to the sign-in page)"
        return None

    def _judge(self, resp: Any, status: int, expect: tuple[int, ...], html: bool,
               check: Any) -> tuple[bool, str, str]:
        if status == 401 and (resp.headers or {}).get("x-signed-out") == "elsewhere" and self.signed_in:
            self.elsewhere = True
            return False, f"{ELSEWHERE} (401 X-Signed-Out: elsewhere)", ""
        if status not in expect:
            if status == 429:
                return False, "429 rate limited (a real user would be throttled)", ""
            if status >= 500:
                return False, f"{status} server error", ""
            return False, f"unexpected status {status}", ""
        if status >= 400:
            return True, "", ""  # an expected refusal (abuser classes, shop refusals)
        if html and status == 200:
            text = resp.text or ""
            if ERROR_PAGE.search(text[:3000]):
                return False, "error page with status 200", ""
            marker = next((m for m in XSS_MARKERS if m in text), None)
            if marker is not None:  # the seeded junk must be rendered escaped (ltlib/junk.py)
                return False, f"unescaped junk markup in the page (XSS): {marker}", ""
        if check is not None:
            res = check(resp)
            why, note = (res if isinstance(res, tuple) else (res, ""))
            if why:
                return False, why, note or ""
            return True, "", note or ""
        return True, "", ""

    def _after_html(self, text: str, url: str) -> None:
        m = CSRF_META.search(text)
        if m:
            self.csrf = m.group(1)
        self.last_url = url

    @staticmethod
    def _path(location: str) -> str:
        if location.startswith(("http://", "https://")):
            parts = urlsplit(location)
            return parts.path + (f"?{parts.query}" if parts.query else "")
        return location

    # ------------------------------------------------------------------------------------------------ page helpers
    def page(self, url: str, name: str, *, expect: tuple[int, ...] = (200,), assets: bool = True,
             check: Any = None, allow_redirect_to: tuple[str, ...] | None = None, forms: bool = False) -> Any:
        """A full page view: the HTML (redirects followed) plus what a browser fetches with it (CSS/JS/favicon and a
        few images - from its cache while fresh, revalidated after that) and, signed in, the notification badge count
        (cached 30 s like the island). ``forms``: a form of this page is submitted next (its submit ids are kept)."""
        resp = self.request("GET", url, name=name, expect=expect, check=check, allow_redirect_to=allow_redirect_to)
        self.form_html = (resp.text or "") if forms and resp is not None and resp.status_code == 200 else ""
        if resp is not None and resp.status_code == 200 and assets:
            self.subresources(resp.text or "")
        if self.signed_in and time.monotonic() - self.unread_at > 30:
            self.unread_at = time.monotonic()
            self.request("GET", "/api/notifications/unread", name="GET /api/notifications/unread",
                         headers=self.api_headers(), html=False)
        return resp

    def _fetch_asset(self, path: str, name: str, accept: str) -> None:
        now = time.time()
        known = self.assets.get(path)
        if known is not None and now - known["at"] < known["fresh"]:
            return  # fresh in the browser cache: no request at all
        extra: dict[str, str] = {}
        if known is not None:
            if known.get("etag"):
                extra["If-None-Match"] = known["etag"]
            if known.get("lm"):
                extra["If-Modified-Since"] = known["lm"]
            name = f"{name} [revalidate]"
        resp = self.request("GET", path, name=name, headers=self.headers(accept=accept, extra=extra), html=False,
                            expect=(200, 304) if known is not None else (200,))
        if resp is None or resp.status_code not in (200, 304):
            return
        h = resp.headers or {}
        fresh = 0.0
        cc = (h.get("cache-control") or "").lower()
        m = re.search(r"max-age=(\d+)", cc)
        if "no-cache" in cc or "no-store" in cc:
            fresh = 0.0
        elif m:
            fresh = float(m.group(1))
        else:
            try:
                lm = parsedate_to_datetime(h.get("last-modified") or (known or {}).get("lm") or "").timestamp()
                date = parsedate_to_datetime(h.get("date")).timestamp() if h.get("date") else now
                fresh = max(0.0, (date - lm) * HEURISTIC_FRESHNESS)
            except (TypeError, ValueError):
                fresh = 0.0
        self.assets[path] = {"at": now, "fresh": fresh, "etag": h.get("etag") or (known or {}).get("etag"),
                             "lm": h.get("last-modified") or (known or {}).get("lm")}

    def subresources(self, text: str) -> None:
        for path in dict.fromkeys(ASSET_RE.findall(text)):
            self._fetch_asset(path, asset_name(path), "*/*")
        n = 0
        for path in dict.fromkeys(MEDIA_RE.findall(text)):
            if n >= MEDIA_PER_PAGE:
                break
            n += 1
            self._fetch_asset(path, "GET /media/public/[image]", "image/avif,image/webp,*/*")

    def form_post(self, url: str, fields: dict[str, Any], *, name: str, expect: tuple[int, ...] = (200,),
                  allow_login: bool = False, check: Any = None) -> Any:
        """A plain HTML form POST (urlencoded, csrf_token + page_id like 00-core.js stamps, the form's own one-time
        submit_id from the page it is on) -> 303 -> page."""
        data = {"csrf_token": self.csrf, **fields}
        data.setdefault("page_id", self.page_id)
        if not data.get("submit_id"):
            data["submit_id"] = self.submit_id_for(url, fields)
        h = self.headers(extra={"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin})
        return self.request("POST", url, name=name, body=urlencode(data), headers=h, expect=expect, allow_login=allow_login,
                            check=check)

    def api_get(self, url: str, name: str, *, expect: tuple[int, ...] = (200,), check: Any = None) -> Any:
        return self.request("GET", url, name=name, headers=self.api_headers(), html=False, expect=expect, check=check)

    def api_post_json(self, url: str, payload: Any, name: str, *, expect: tuple[int, ...] = (200,),
                      check: Any = None, accept: str | None = None) -> Any:
        h = self.api_headers(unsafe=True, json_body=True)
        if accept:
            h["Accept"] = accept
        return self.request("POST", url, name=name, body=json.dumps(payload), headers=h, html=False, expect=expect,
                            follow=False, check=check)

    def api_post_form(self, url: str, fields: dict[str, Any], name: str, *, expect: tuple[int, ...] = (200,)) -> Any:
        """IP.api(form.action, {method: "POST", form}) - multipart FormData, JSON answer."""
        body, ctype = multipart({"csrf_token": self.csrf, **fields})
        h = self.api_headers(unsafe=True)
        h["Content-Type"] = ctype
        return self.request("POST", url, name=name, body=body, headers=h, html=False, expect=expect, follow=False)


# ------------------------------------------------------------------------------------------------ stats names
_ROUTES = [
    (re.compile(r"^/gyms/[^/?]+/branches/\d+"), "/gyms/[slug]/branches/[id]"),
    (re.compile(r"^/gyms/[^/?]+/branches"), "/gyms/[slug]/branches"),
    (re.compile(r"^/gyms/[^/?]+"), "/gyms/[slug]"),
    (re.compile(r"^/shop/p/\d+/[^/?]+"), "/shop/p/[id]/[slug]"),
    (re.compile(r"^/shop/p/\d+"), "/shop/p/[id]"),
    (re.compile(r"^/shop/orders/\d+/test-gateway"), "/shop/orders/[id]/test-gateway"),
    (re.compile(r"^/shop/orders/\d+"), "/shop/orders/[id]"),
    (re.compile(r"^/shop/gym/[^/?]+"), "/shop/gym/[slug]"),
    (re.compile(r"^/chat/\d+"), "/chat/[id]"),
    (re.compile(r"^/jobs/\d+"), "/jobs/[id]"),
    (re.compile(r"^/owner/jobs/\d+/enquiries"), "/owner/jobs/[id]/enquiries"),
    (re.compile(r"^/owner/shop/orders/\d+"), "/owner/shop/orders/[id]"),
    (re.compile(r"^/owner/customers/\d+"), "/owner/customers/[id]"),
    (re.compile(r"^/owner/staff/\d+"), "/owner/staff/[id]"),
    (re.compile(r"^/staff/clients/\d+"), "/staff/clients/[id]"),
    (re.compile(r"^/profile/\d+"), "/profile/[id]"),
]


def route_name(path: str) -> str:
    base = path.split("?", 1)[0]
    for rx, label in _ROUTES:
        if rx.match(base):
            return "GET " + label
    return "GET " + base


def asset_name(path: str) -> str:
    base = path.split("?", 1)[0]
    if base.startswith("/static/dist/") or base.startswith("/static/img/"):
        return "GET " + base
    return "GET /static/[other]"
