"""The local AI fallback (Gemma in Ollama) as it runs: started by run.sh / run.ps1 (scripts/ai_fallback.py via
``manage.py ai-fallback``), defined in docker/compose.yml, checked by ``manage.py ai-status`` / ``ai-test`` / ``check``,
plus two production fixes of the same round (the image's health check, ``manage.py run --workers``' trust list).

* the start-up decision for every situation (Ollama ready / model missing / Docker running / Docker Desktop stopped /
  native Ollama / nothing installed / remote URL / not in the chain), one line of output, slow work detached in the
  background and never blocking; the background helper waits for Docker Desktop, skips a pull that is not needed;
* compose: healthcheck, named model volume, restart policy, memory limits, log caps, an idempotent pull, the GPU
  override, http://ollama:11434/v1 for the app inside compose (validated by `docker compose config` when available);
* the image health check passes with FORCE_HTTPS on (X-Forwarded-Proto from 127.0.0.1 + an allowed Host);
* ``manage.py run --workers N`` hands uvicorn TRUSTED_PROXIES (never '*'), not $FORWARDED_ALLOW_IPS;
* the provider status: up, model pulled, a 1-token latency probe; ``ai-test`` through the app's own provider code.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai import client as ai_client
from app.ai.client import FakeProvider, OpenAICompatProvider, probe_provider
from app.config import get_settings
from tests.ai_standin import StandIn, free_port

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:11434/v1"
MODEL = "gemma3:4b"


def _load(name: str, path: Path) -> Any:
    key = f"ironpulse_scripts_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


fallback = _load("ai_fallback", ROOT / "scripts" / "ai_fallback.py")
manage = _load("manage", ROOT / "scripts" / "manage.py")
check_env = _load("check_env", ROOT / "scripts" / "check_env.py")
healthcheck = _load("docker_healthcheck", ROOT / "docker" / "healthcheck.py")


class StubProbes(fallback.Probes):
    """What this computer has: Ollama answering (``models``: None = not running), a docker CLI, its engine running,
    Docker Desktop installed, a native ollama CLI."""

    def __init__(self, *, models: list[str] | None = None, docker: str | None = None, running: bool = False,
                 desktop: str | None = None, cli: str | None = None) -> None:
        super().__init__()
        self._models, self._docker, self._running, self._desktop, self._cli = models, docker, running, desktop, cli
        self.running_checks = 0

    def ollama_models(self, base_url: str) -> list[str] | None:
        return self._models

    def docker(self) -> str | None:
        return self._docker

    def docker_running(self, docker: str) -> bool:
        self.running_checks += 1
        return self._running

    def desktop_app(self) -> str | None:
        return self._desktop

    def ollama_cli(self) -> str | None:
        return self._cli


ORDER = ["gateway", "docker", "anthropic"]


# ===================================================================================== the start-up decision
@pytest.mark.parametrize(("probes", "action", "commands"), [
    (StubProbes(models=[MODEL]), "ready", []),
    (StubProbes(models=["gemma3:4b", "llama3:8b"], docker="/bin/docker", running=True), "ready", []),
    (StubProbes(models=[], docker="/bin/docker", running=True), "pull", [["ollama-pull"]]),
    (StubProbes(models=["other:1b"], cli="/usr/local/bin/ollama"), "pull", [["/usr/local/bin/ollama", "pull", MODEL]]),
    (StubProbes(models=[]), "missing", []),
    (StubProbes(docker="/bin/docker", running=True), "compose", [["ollama", "ollama-pull"]]),
    (StubProbes(docker="/bin/docker", running=False, desktop="/Applications/Docker.app"), "desktop",
     [["ollama", "ollama-pull"]]),
    (StubProbes(cli="/usr/local/bin/ollama"), "native", [["/usr/local/bin/ollama", "serve"],
                                                         ["/usr/local/bin/ollama", "pull", MODEL]]),
    (StubProbes(docker="/usr/bin/docker", running=False), "missing", []),
    (StubProbes(), "missing", []),
], ids=["ready", "ready-docker", "pull-compose", "pull-native", "pull-no-tool", "compose", "desktop", "native",
        "docker-stopped-linux", "nothing"])
def test_the_start_up_decision(probes, action, commands):
    d = fallback.decide(ORDER, BASE, MODEL, probes=probes)
    assert d.action == action, d.message
    got = []
    for cmd in d.commands:
        if cmd[1:2] == ["compose"]:  # docker compose --env-file <.env> -f <compose.yml> up -d <services>
            assert cmd[:2] == [probes.docker(), "compose"] and cmd[cmd.index("--env-file") + 1] == str(ROOT / ".env")
            assert cmd[cmd.index("-f") + 1] == str(ROOT / "docker" / "compose.yml")
            got.append(cmd[cmd.index("-d") + 1:])
        else:
            got.append(cmd)
    assert got == commands
    assert "\n" not in d.message  # ONE line
    if action == "ready":
        assert probes.running_checks == 0  # Ollama answers: docker is not even asked (fast start)
    if d.action == "missing" and probes.docker() is None and probes.ollama_cli() is None and probes._models is None:
        assert d.message == fallback.INSTALL_HINT and "Docker Desktop" in d.message and "Ollama" in d.message


def test_nothing_to_start_outside_the_local_chain():
    assert fallback.decide(["gateway", "anthropic"], BASE, MODEL, probes=StubProbes()).action == "skip"
    assert fallback.decide(ORDER, "", MODEL, probes=StubProbes()).action == "skip"
    assert fallback.decide(ORDER, BASE, MODEL, probes=StubProbes(), disabled=True).action == "skip"
    remote = fallback.decide(ORDER, "http://192.168.0.50:11434/v1", MODEL, probes=StubProbes(docker="/bin/docker"))
    assert remote.action == "remote" and not remote.commands
    for local in ("http://127.0.0.1:11434/v1", "http://[::1]:11434/v1", "http://host.docker.internal:11434/v1"):
        assert fallback.is_local(local), local
    assert fallback.has_model(["gemma3:latest"], "gemma3") and not fallback.has_model(["gemma3:12b"], MODEL)


def test_run_prints_one_line_and_starts_the_slow_part_detached(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fallback, "LOG_FILE", tmp_path / "logs" / "ai-fallback.log")
    spawned: list[tuple[list[str], dict[str, Any]]] = []
    popen = lambda cmd, **kw: spawned.append((cmd, kw)) or SimpleNamespace(pid=1)  # noqa: E731
    d = fallback.run(values=(ORDER, BASE, MODEL), probes=StubProbes(docker="/x/docker", running=True), popen=popen)
    out = capsys.readouterr().out.strip().splitlines()
    assert d.action == "compose" and len(out) == 1 and out[0].startswith("[ai] local AI fallback: starting Ollama")
    (cmd, kw), = spawned
    assert cmd[:3] == [sys.executable, str(ROOT / "scripts" / "ai_fallback.py"), "_background"]
    spec = json.loads(cmd[3])
    assert spec["action"] == "compose" and spec["docker"] == "/x/docker" and spec["desktop"] is False
    assert kw["stdin"] == subprocess.DEVNULL and kw["stderr"] == subprocess.STDOUT
    if os.name != "nt":
        assert kw["start_new_session"] is True  # Ctrl+C on the app never kills a model download half-way
    assert kw["env"]["PATH"].split(os.pathsep)[0] == "/x"  # docker's own folder first (its credential helper)

    spawned.clear()
    fallback.run(values=(ORDER, BASE, MODEL), probes=StubProbes(docker="/x/docker", running=True), popen=popen,
                 dry_run=True)
    assert not spawned and "would run in the background:" in capsys.readouterr().out
    fallback.run(values=(ORDER, BASE, MODEL), probes=StubProbes(models=[MODEL]), popen=popen)
    assert not spawned  # ready: nothing to start

    def broken() -> Any:
        raise SystemExit(3)  # app.config.ConfigError: python -m app reports it, the fallback just steps aside

    monkeypatch.setattr(fallback, "settings_values", broken)
    assert fallback.run(popen=popen) is None and "skipped" in capsys.readouterr().out


def test_the_background_helper_waits_for_docker_desktop_and_skips_needless_pulls(monkeypatch, capsys):
    class Waking(StubProbes):
        def docker_running(self, docker: str) -> bool:
            self.running_checks += 1
            return self.running_checks >= 3  # Docker Desktop takes a moment

    probes = Waking(docker="/x/docker", desktop="/Applications/Docker.app", models=[MODEL])
    launched: list[list[str]] = []
    called: list[list[str]] = []
    monkeypatch.setattr(fallback.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(fallback.subprocess, "call", lambda cmd, **kw: called.append(cmd) or 0)
    monkeypatch.setattr(fallback.time, "sleep", lambda s: None)
    spec = {"action": "desktop", "message": "m", "docker": "/x/docker", "desktop": True, "base_url": BASE,
            "model": MODEL, "commands": [["/x/docker", "compose", "up", "-d", "ollama", "ollama-pull"],
                                         ["/x/ollama", "pull", MODEL]]}
    assert fallback.background(spec, probes) == 0
    assert launched and launched[0][-1] == "/Applications/Docker.app"  # Docker Desktop started in the background
    assert probes.running_checks == 3 and called == [spec["commands"][0]]  # the pull was skipped: model present
    assert "Docker is running" in capsys.readouterr().out

    never = StubProbes(docker="/x/docker", desktop="/Applications/Docker.app")
    monkeypatch.setattr(fallback, "DOCKER_WAIT_SECONDS", 0)
    assert fallback.background({**spec, "commands": []}, never) == 1 and "did not start" in capsys.readouterr().out


def test_docker_is_found_where_docker_desktop_puts_it(tmp_path):
    exe = tmp_path / ".docker" / "bin" / "docker"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert fallback.find_docker(lambda name: None, home=tmp_path, platform="darwin") == str(exe)
    assert fallback.find_docker(lambda name: "/usr/bin/docker", home=tmp_path) == "/usr/bin/docker"
    assert fallback.tool_env(str(exe))["PATH"].split(os.pathsep)[0] == str(exe.parent)


def test_manage_ai_fallback_command(monkeypatch, capsys):
    monkeypatch.setattr(fallback, "settings_values", lambda: (ORDER, BASE, MODEL))
    monkeypatch.setattr(fallback, "Probes", lambda: StubProbes(models=[MODEL]))
    assert manage.main(["ai-fallback", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "ready"
    assert manage.main(["ai-fallback", "--no-ai-docker"]) == 0
    assert capsys.readouterr().out == ""  # skipped silently


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash")
def test_run_sh_asks_for_the_fallback_unless_told_not_to():
    def sh(*args: str) -> str:
        return subprocess.run(["bash", str(ROOT / "run.sh"), "--dry-run", "--no-services", *args], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60).stdout

    assert "[ai] " in sh() and "local AI fallback" in sh()  # its one line, whatever this computer has
    assert "[ai]" not in sh("--no-ai-docker")
    text = (ROOT / "run.sh").read_text(encoding="utf-8")
    assert '"$MANAGE" ai-fallback || true' in text  # never stops the start


# ===================================================================================== compose
def test_compose_defines_a_production_ready_ollama():
    text = (ROOT / "docker" / "compose.yml").read_text(encoding="utf-8")
    block = text[text.index("\n  ollama:\n"):text.index("\n  ollama-pull:\n")]
    for needle in ("restart: unless-stopped", "ollama:/root/.ollama", 'test: ["CMD", "ollama", "list"]',
                   "memory: ${OLLAMA_MEMORY_LIMIT:-6g}", "memory: ${OLLAMA_MEMORY_RESERVATION:-4g}", 'max-size: "10m"',
                   'OLLAMA_NUM_PARALLEL: "1"', 'OLLAMA_MAX_LOADED_MODELS: "1"', "# devices:", "#   - driver: nvidia",
                   "127.0.0.1:11434:11434", "http://ollama:11434/v1"):
        assert needle in block or needle in text, needle
    pull = text[text.index("\n  ollama-pull:\n"):text.index("\n  kokoro:")]
    assert 'ollama show "$$MODEL"' in pull and '|| ollama pull "$$MODEL"' in pull and 'restart: "no"' in pull


DOCKER_CLI = fallback.find_docker()


@pytest.mark.skipif(DOCKER_CLI is None, reason="docker is not installed")
def test_compose_validates_and_merges_with_the_gpu_override(tmp_path):
    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    def config(*files: str, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        args = [DOCKER_CLI, "compose", "--env-file", str(env)]
        for name in files:
            args += ["-f", str(ROOT / "docker" / name)]
        proc = subprocess.run([*args, "--profile", "app", "--profile", "voice", "config", "--format", "json"],
                              capture_output=True, text=True, timeout=60, env={**os.environ, **(extra_env or {})})
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    d = config("compose.yml")
    ollama, pull = d["services"]["ollama"], d["services"]["ollama-pull"]
    assert ollama["restart"] == "unless-stopped" and ollama["healthcheck"]["test"] == ["CMD", "ollama", "list"]
    assert ollama["deploy"]["resources"]["limits"]["memory"] == str(6 * 1024**3)
    assert ollama["deploy"]["resources"]["reservations"]["memory"] == str(4 * 1024**3)
    assert ollama["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    assert any(v["source"] == "ollama" and v["target"] == "/root/.ollama" for v in ollama["volumes"])
    assert pull["entrypoint"] == ["/bin/sh", "-c"] and pull["environment"]["MODEL"] == MODEL
    assert pull["depends_on"]["ollama"]["condition"] == "service_healthy" and pull["restart"] == "no"
    assert d["services"]["app"]["environment"]["AI_DOCKER_BASE_URL"] == "http://ollama:11434/v1"
    gpu = config("compose.yml", "compose.gpu.yml")["services"]["ollama"]["deploy"]["resources"]["reservations"]
    assert gpu["memory"] == str(4 * 1024**3) and gpu["devices"][0]["driver"] == "nvidia"
    bigger = config("compose.yml", extra_env={"OLLAMA_MEMORY_LIMIT": "12g", "AI_DOCKER_MODEL": "gemma3:12b"})
    assert bigger["services"]["ollama"]["deploy"]["resources"]["limits"]["memory"] == str(12 * 1024**3)
    assert bigger["services"]["ollama-pull"]["environment"]["MODEL"] == "gemma3:12b"


# ===================================================================================== the image's health check
class _Recorder(http.server.BaseHTTPRequestHandler):
    status = 200
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(self.status)
        if self.status == 308:
            self.send_header("Location", "https://example/healthz")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        pass


class _LocalServer(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:  # HTTPServer would resolve its own name (getfqdn): seconds on a busy Mac
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def test_the_image_health_check_passes_with_forced_https():
    server = _LocalServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _Recorder.seen.clear()
        assert healthcheck.check(port, "gym.example.com,localhost") == (True, "HTTP 200")
        assert _Recorder.seen[-1]["host"] == "gym.example.com" and _Recorder.seen[-1]["x-forwarded-proto"] == "https"
        assert healthcheck.check(port, "*.example.com")[0] and _Recorder.seen[-1]["host"] == "health.example.com"
        assert healthcheck.check(port, None)[0] and _Recorder.seen[-1]["host"] == "localhost"
        _Recorder.status = 503
        assert healthcheck.check(port, "localhost") == (False, "HTTP 503")
        _Recorder.status = 308
        ok, why = healthcheck.check(port, "localhost")
        assert not ok and "TRUSTED_PROXIES" in why and len(_Recorder.seen) == 5  # a redirect is never followed
    finally:
        _Recorder.status = 200
        server.shutdown()
    assert not healthcheck.check(free_port(12399), "localhost", timeout=1)[0]  # nothing listens
    assert healthcheck.probe_host("::1,localhost") == "[::1]"


async def test_the_health_probe_passes_the_https_redirect_of_the_real_app(monkeypatch):
    """The app with FORCE_HTTPS + TRUST_PROXY_HEADERS (production defaults): a plain probe gets the 308, the probe's
    request (X-Forwarded-Proto: https from 127.0.0.1, a TRUSTED_PROXIES address) gets /healthz."""
    from app.security import talisman
    from tests.conftest import make_client

    secure = get_settings().model_copy(update={"force_https": True, "trust_proxy_headers": True})
    monkeypatch.setattr(talisman, "get_settings", lambda: secure)
    async with make_client("127.0.0.1") as c:
        plain = await c.get("/healthz")
        assert plain.status_code == 308 and plain.headers["location"].startswith("https://")
        probed = await c.get("/healthz", headers={"X-Forwarded-Proto": "https"})
        assert probed.status_code == 200 and probed.json()["database"] is True
    async with make_client("203.0.113.9") as c:  # anyone else cannot claim HTTPS with the header
        assert (await c.get("/healthz", headers={"X-Forwarded-Proto": "https"})).status_code == 308


# ===================================================================================== manage.py run --workers
def test_run_workers_hands_uvicorn_our_trust_list(monkeypatch, capsys):
    calls: list[list[str]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append(list(args)) or 0)

    def settings(**kw: Any) -> Any:
        return SimpleNamespace(host="127.0.0.1", port=10000, redis_url="redis://x", trust_proxy_headers=True,
                               trusted_proxies=["127.0.0.1", "::1"], **kw)

    monkeypatch.setattr(manage, "load_settings", lambda: settings())
    assert manage.main(["run", "--workers", "2"]) == 0
    assert calls[-1][calls[-1].index("--forwarded-allow-ips") + 1] == "127.0.0.1,::1" and "--proxy-headers" in calls[-1]
    for proxies, expected in ((["10.0.0.0/8", "*", "proxy.example", " 172.16.0.0/12 "], "10.0.0.0/8,172.16.0.0/12"),
                              (["*"], "127.0.0.1"), ([], "127.0.0.1")):
        monkeypatch.setattr(manage, "load_settings", lambda p=proxies: SimpleNamespace(
            host="127.0.0.1", port=10000, redis_url="redis://x", trust_proxy_headers=True, trusted_proxies=p))
        assert manage.main(["run", "--workers", "2"]) == 0
        assert calls[-1][calls[-1].index("--forwarded-allow-ips") + 1] == expected, proxies  # never uvicorn's '*'
    monkeypatch.setattr(manage, "load_settings", lambda: SimpleNamespace(
        host="127.0.0.1", port=10000, redis_url="redis://x", trust_proxy_headers=False, trusted_proxies=["*"]))
    assert manage.main(["run", "--workers", "2"]) == 0
    assert "--no-proxy-headers" in calls[-1] and "--forwarded-allow-ips" not in calls[-1]
    capsys.readouterr()


# ===================================================================================== status + test commands
@pytest.fixture
async def standin():
    server = await StandIn("docker", port=free_port(12303), words=["OK"]).start()
    yield server
    await server.stop()


async def test_probe_provider_through_the_apps_own_provider_code(standin):
    provider = OpenAICompatProvider(get_settings(), name="docker", label="Gemma (Docker failsafe)",
                                    base_url=standin.base_url, api_key="k", model="gemma", timeout=10,
                                    max_concurrency=1)
    standin.mode = "length"  # max_tokens=1: finish_reason 'length' is the expected end, not a failure
    r = await probe_provider(provider)
    assert r["ok"] and r["text"] == "OK" and r["first_ms"] is not None and r["error"] is None
    assert standin.requests[-1]["body"]["max_tokens"] == 1 and standin.requests[-1]["stream"] is True
    standin.mode = "error503"
    r = await probe_provider(provider)
    assert not r["ok"] and "HTTP 503" in r["error"]
    await standin.stop()
    r = await probe_provider(provider)
    assert not r["ok"] and "unreachable" in r["error"]
    assert (await probe_provider(OpenAICompatProvider(get_settings(), name="docker", label="x", base_url="",
                                                      api_key="", model="m", timeout=1, max_concurrency=1)))[
        "error"] == "not configured"


def test_manage_ai_test_reports_each_provider(monkeypatch, capsys):
    monkeypatch.setattr(manage, "load_settings", lambda: get_settings().model_copy(
        update={"ai_provider_order": ["gateway", "docker"]}))
    providers = {"gateway": FakeProvider("gateway", fail=True), "docker": FakeProvider("docker", chunks=["OK"])}
    monkeypatch.setattr(ai_client, "build_provider", lambda s, name: providers[name])
    assert manage.main(["ai-test"]) == 1  # one of them failed
    out = capsys.readouterr().out
    assert "gateway   FAIL" in out and "simulated outage" in out and "docker    OK" in out and "first token" in out
    assert manage.main(["ai-test", "--provider", "docker"]) == 0
    assert "gateway" not in capsys.readouterr().out


def test_ai_status_shows_up_model_pulled_and_latency(monkeypatch):
    def get(url: str, headers: Any = None, timeout: float = 3.0) -> Any:
        if url.startswith("http://gw"):
            return None, None, "URLError: [Errno 64] Host is down"
        return 200, {"data": [{"id": MODEL}]}, None

    posts: list[dict[str, Any]] = []

    def post(url: str, payload: dict[str, Any], headers: Any = None, timeout: float = 60.0) -> Any:
        posts.append(payload)
        return 200, {"choices": [{"message": {"content": "OK"}}]}, None

    monkeypatch.setattr(check_env, "http_get", get)
    monkeypatch.setattr(check_env, "http_post_json", post)
    settings = SimpleNamespace(ai_provider_order=ORDER, ai_gateway_base_url="http://gw:8000/v1",
                               ai_gateway_api_key="k", ai_gateway_model="gemma", ai_gateway_status_url=None,
                               ai_docker_base_url=BASE, ai_docker_api_key="ollama", ai_docker_model=MODEL,
                               anthropic_api_key=None, anthropic_model="claude")
    ctx = check_env.Ctx(network=True, db=False, settings=settings, load_app_settings=False, ai_probe=True)
    rows = {r.name: r for r in check_env.check_ai(ctx)}
    assert rows["Gemma in Docker"].status == check_env.OK and "1-token answer in" in rows["Gemma in Docker"].detail
    assert posts == [{"model": MODEL, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 1,
                      "temperature": 0}]
    assert rows["Gemma gateway"].status == check_env.WARN and "unreachable" in rows["Gemma gateway"].detail
    assert "answering now: docker" in rows["AI failover chain"].detail

    monkeypatch.setattr(check_env, "http_post_json", lambda *a, **k: (500, None, "HTTP 500"))
    rows = {r.name: r for r in check_env.check_ai(ctx)}
    assert rows["Gemma in Docker"].status == check_env.WARN and "did not answer" in rows["Gemma in Docker"].detail
    assert rows["AI failover chain"].status == check_env.WARN  # nothing answers

    ctx.ai_probe = False  # `manage.py check` without --ai-probe: no completion is requested
    posts.clear()
    monkeypatch.setattr(check_env, "http_post_json", post)
    rows = {r.name: r for r in check_env.check_ai(ctx)}
    assert not posts and rows["Gemma in Docker"].detail == f"{BASE} · {MODEL} ready"


def test_ai_status_says_what_could_run_the_fallback(monkeypatch):
    monkeypatch.setattr(check_env, "http_get", lambda *a, **k: (None, None, "ConnectionRefusedError"))
    monkeypatch.setattr(fallback, "Probes", lambda: StubProbes(docker="/x/docker", running=False))
    ctx = check_env.Ctx(network=True, db=False, load_app_settings=False, settings=SimpleNamespace(
        ai_provider_order=["docker"], ai_docker_base_url=BASE, ai_docker_api_key="ollama", ai_docker_model=MODEL,
        ai_gateway_base_url=None, anthropic_api_key=None))
    row = next(r for r in check_env.check_ai(ctx) if r.name == "Gemma in Docker")
    assert "Docker installed, NOT running" in row.detail and "./run.sh starts it" in row.fix


# ===================================================================================== README "Limits"
def test_the_limits_table_states_the_fallback_limits():
    """The rows this package added to README "Limits" state the values in the code (like tests/test_limits_docs.py)."""
    from app.ai import simulated

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    table = text[text.index("\n## Limits\n"):text.index("\n## ", text.index("\n## Limits\n") + 10)]
    rows = {line.split(" | ")[0].strip("| ").strip(): line for line in table.splitlines() if line.startswith("| **")}
    lan = _load("lan", ROOT / "scripts" / "lan.py")
    s = get_settings()
    failover = rows["**AI** · failover between providers"]
    assert f"gets {ai_client.CONNECT_TIMEOUT_SECONDS:g} s" in failover
    assert f"(Claude {ai_client.CLOUD_CONNECT_TIMEOUT_SECONDS:g} s)" in failover
    assert f"{s.ai_circuit_failures} failures in a row skip it for {s.ai_circuit_cooldown_seconds} s" in failover
    compose = (ROOT / "docker" / "compose.yml").read_text(encoding="utf-8")
    local = rows["**AI** · local fallback (Ollama in Docker)"]
    assert s.ai_docker_max_concurrency == 1 and "one answer at a time" in local
    assert "OLLAMA_MEMORY_LIMIT:-6g" in compose and "6 GB of memory" in local
    assert "OLLAMA_MEMORY_RESERVATION:-4g" in compose and "(4 GB reserved)" in local
    assert 'max-size: "10m"' in compose and 'max-file: "3"' in compose and "3 × 10 MB" in local
    aids = rows["**AI** · simulated provider's bug-hunt aids"]
    assert f"{simulated.CAPTURE_MAX_MB} MB" in aids and f"at most {simulated.MAX_NONCES} nonces" in aids
    certs = rows["**Network** · LAN mode (`./run.sh --lan`) certificates"]
    assert f"valid {lan.LEAF_DAYS} days" in certs and f"renewed {lan.RENEW_DAYS} days before" in certs
    assert f"the local CA {lan.CA_DAYS} days" in certs


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_start_scripts_are_shellcheck_clean():
    proc = subprocess.run(["shellcheck", "-s", "bash", str(ROOT / "run.sh"), str(ROOT / "setup.sh")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout
