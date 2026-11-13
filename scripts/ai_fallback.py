"""Start the local AI fallback - Gemma in Ollama - next to IronPulse (run.sh / run.ps1 call ``manage.py ai-fallback``).

The AI router tries AI_PROVIDER_ORDER (default gateway -> docker -> anthropic); 'docker' is Ollama at AI_DOCKER_BASE_URL
(default http://localhost:11434/v1) with AI_DOCKER_MODEL (gemma3:4b). This makes sure it runs, and never delays the
app's start: everything slow (starting Docker Desktop, starting the container, downloading the model) happens in a
background process that logs to var/logs/ai-fallback.log. What it decides (``decide``):

  skip      'docker' is not in AI_PROVIDER_ORDER, AI_DOCKER_BASE_URL is empty, or --no-ai-docker
  remote    AI_DOCKER_BASE_URL is another computer: nothing to start here
  ready     Ollama already answers and has the model
  pull      Ollama answers but the model is missing: pulled in the background (compose ollama-pull or ollama pull)
  compose   Docker runs: ``docker compose up -d ollama ollama-pull`` in the background (the pull is skipped when the
            model is already in the volume)
  desktop   Docker Desktop is installed but not running: started in the background (macOS / Windows), then compose
  native    the ollama CLI is installed (no Docker): ``ollama serve`` + ``ollama pull`` in the background
  missing   neither: ONE line saying what to install - IronPulse starts anyway (gateway / Claude / 'AI unavailable')

    python scripts/manage.py ai-fallback              decide, start in the background, print one line
    python scripts/manage.py ai-fallback --dry-run    print the decision and the commands; start nothing
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker" / "compose.yml"
LOG_FILE = ROOT / "var" / "logs" / "ai-fallback.log"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})
DOCKER_WAIT_SECONDS = 180  # how long the background helper waits for a Docker Desktop that is starting
OLLAMA_WAIT_SECONDS = 60  # ... and for a native `ollama serve` to answer
PROBE_TIMEOUT = 1.5
INSTALL_HINT = ("local AI fallback not available: install Docker Desktop (then ./setup.sh --with-ai-docker) or Ollama "
                "(https://ollama.com) - IronPulse starts anyway (Gemma gateway / Claude, else 'AI unavailable')")


def docker_candidates(home: Path | None = None, platform: str = sys.platform) -> list[Path]:
    """Where Docker Desktop puts the docker CLI when it is not on PATH (a terminal without ~/.zprofile, VS Code)."""
    home = home or Path.home()
    if platform.startswith("win"):
        return [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "resources" / "bin"
                / "docker.exe"]
    return [home / ".docker" / "bin" / "docker", Path("/usr/local/bin/docker"), Path("/opt/homebrew/bin/docker"),
            Path("/Applications/Docker.app/Contents/Resources/bin/docker"), Path("/usr/bin/docker")]


def find_docker(which: Callable[[str], str | None] = shutil.which, home: Path | None = None,
                platform: str = sys.platform) -> str | None:
    found = which("docker")
    if found:
        return found
    return next((str(p) for p in docker_candidates(home, platform) if p.is_file() and os.access(p, os.X_OK)), None)


def docker_desktop_app(platform: str = sys.platform) -> str | None:
    """Docker Desktop's launcher when it is installed (to start it in the background)."""
    if platform == "darwin":
        for app in (Path("/Applications/Docker.app"), Path.home() / "Applications" / "Docker.app"):
            if app.exists():
                return str(app)
    elif platform.startswith("win"):
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
        if exe.exists():
            return str(exe)
    return None


def tool_env(docker: str | None) -> dict[str, str]:
    """The environment for docker commands: its own folder on PATH (Docker Desktop's credential helper lives there -
    without it image pulls fail with 'error getting credentials')."""
    env = dict(os.environ)
    if docker:
        folder = str(Path(docker).parent)
        if folder not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = folder + os.pathsep + env.get("PATH", "")
    return env


# ============================================================================= probes (tests replace them)
@dataclass
class Probes:
    which: Callable[[str], str | None] = shutil.which
    platform: str = sys.platform
    home: Path | None = None

    def get_json(self, url: str, timeout: float = PROBE_TIMEOUT) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "ironpulse-ai-fallback/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - our own .env URL
                return json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def ollama_models(self, base_url: str) -> list[str] | None:
        """The models Ollama serves (None: nothing answers at ``base_url``)."""
        data = self.get_json(base_url.rstrip("/") + "/models")
        if not isinstance(data, dict):
            return None
        names = [str(m.get("id")) for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]
        return names

    def docker(self) -> str | None:
        return find_docker(self.which, self.home, self.platform)

    def docker_running(self, docker: str) -> bool:
        try:
            proc = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                                  timeout=8, env=tool_env(docker))
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def desktop_app(self) -> str | None:
        return docker_desktop_app(self.platform)

    def ollama_cli(self) -> str | None:
        return self.which("ollama")


def has_model(models: list[str], model: str) -> bool:
    want = model if ":" in model else f"{model}:latest"
    return any(m == model or m == want for m in models)


def is_local(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.startswith("127.")


# ============================================================================= the decision
@dataclass
class Decision:
    action: str  # skip | remote | ready | pull | compose | desktop | native | missing
    message: str
    commands: list[list[str]] = field(default_factory=list)  # run by the background helper, in order
    docker: str | None = None
    model: str = ""
    base_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "message": self.message, "commands": self.commands, "model": self.model,
                "base_url": self.base_url}


def compose_cmd(docker: str, *services: str) -> list[str]:
    return [docker, "compose", "--env-file", str(ROOT / ".env"), "-f", str(COMPOSE_FILE), "up", "-d", *services]


def decide(order: list[str], base_url: str | None, model: str, *, probes: Probes | None = None,
           disabled: bool = False) -> Decision:
    probes = probes or Probes()
    base_url = (base_url or "").strip()
    if disabled or "docker" not in order or not base_url:
        why = "--no-ai-docker" if disabled else ("AI_DOCKER_BASE_URL is empty" if base_url == "" and "docker" in order
                                                 else "'docker' is not in AI_PROVIDER_ORDER")
        return Decision("skip", f"local AI fallback skipped ({why})", model=model, base_url=base_url)
    if not is_local(base_url):
        return Decision("remote", f"local AI fallback: AI_DOCKER_BASE_URL={base_url} is another computer - nothing to "
                                  "start here", model=model, base_url=base_url)
    models = probes.ollama_models(base_url)
    if models is not None and has_model(models, model):
        return Decision("ready", f"local AI fallback ready: Ollama at {base_url} serves {model}", model=model,
                        base_url=base_url)
    docker = probes.docker()
    running = bool(docker) and probes.docker_running(docker)  # type: ignore[arg-type]
    if models is not None:  # Ollama answers, the model is missing
        if docker and running:
            return Decision("pull", f"local AI fallback: Ollama is up, pulling {model} in the background (~3.3 GB once) "
                                    f"- log: {_rel(LOG_FILE)}", [compose_cmd(docker, "ollama-pull")], docker, model,
                            base_url)
        cli = probes.ollama_cli()
        if cli:
            return Decision("pull", f"local AI fallback: Ollama is up, pulling {model} in the background - log: "
                                    f"{_rel(LOG_FILE)}", [[cli, "pull", model]], docker, model, base_url)
        return Decision("missing", f"local AI fallback: Ollama at {base_url} answers but {model} is not pulled - run: "
                                   "docker compose --env-file .env -f docker/compose.yml up ollama-pull",
                        model=model, base_url=base_url)
    if docker and running:
        return Decision("compose", f"local AI fallback: starting Ollama in Docker in the background ({model}; the first "
                                   f"start downloads ~3.3 GB) - log: {_rel(LOG_FILE)}",
                        [compose_cmd(docker, "ollama", "ollama-pull")], docker, model, base_url)
    if docker and probes.desktop_app():
        return Decision("desktop", f"local AI fallback: starting Docker Desktop, then Ollama, in the background - log: "
                                   f"{_rel(LOG_FILE)}", [compose_cmd(docker, "ollama", "ollama-pull")], docker, model,
                        base_url)
    cli = probes.ollama_cli()
    if cli:
        return Decision("native", f"local AI fallback: starting the installed Ollama in the background ({model}) - log: "
                                  f"{_rel(LOG_FILE)}", [[cli, "serve"], [cli, "pull", model]], docker, model, base_url)
    if docker:  # a docker CLI without a running engine and no Desktop to start (Linux): one clear line
        return Decision("missing", "local AI fallback not started: Docker is installed but not running - start it "
                                   "(sudo systemctl start docker), then ./run.sh again", docker=docker, model=model,
                        base_url=base_url)
    return Decision("missing", INSTALL_HINT, model=model, base_url=base_url)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ============================================================================= background work
def spawn_background(decision: Decision, *, popen: Callable[..., Any] = subprocess.Popen) -> Any:
    """Run ``_background`` for this decision in a detached process (it outlives nothing: it only starts things)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
    cmd = [sys.executable, str(Path(__file__).resolve()), "_background", json.dumps(decision.as_dict() | {
        "docker": decision.docker, "desktop": decision.action == "desktop"})]
    kwargs: dict[str, Any] = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                              "cwd": str(ROOT), "env": tool_env(decision.docker)}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True  # Ctrl+C on the app does not kill a model download half-way
    try:
        return popen(cmd, **kwargs)
    finally:
        log.close()


def _stamp(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def background(spec: dict[str, Any], probes: Probes | None = None) -> int:
    """The detached helper: start Docker Desktop if asked, wait for it, then run the commands (``native``: start
    ``ollama serve`` detached and wait until it answers before pulling)."""
    probes = probes or Probes()
    docker = spec.get("docker")
    _stamp(f"ai-fallback: {spec.get('action')} - {spec.get('message')}")
    if spec.get("desktop"):
        app = probes.desktop_app()
        if app:
            launcher = ["open", "-g", "-a", app] if sys.platform == "darwin" else [app]
            subprocess.Popen(launcher, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        deadline = time.monotonic() + DOCKER_WAIT_SECONDS
        while not probes.docker_running(docker):
            if time.monotonic() > deadline:
                _stamp(f"ai-fallback: Docker did not start within {DOCKER_WAIT_SECONDS} s - giving up (start Docker "
                       "Desktop, then ./run.sh again)")
                return 1
            time.sleep(3)
        _stamp("ai-fallback: Docker is running")
    for cmd in spec.get("commands") or []:
        if cmd[1:2] == ["serve"]:  # native ollama: a server that keeps running on its own
            subprocess.Popen(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,  # noqa: S603
                             start_new_session=os.name != "nt")
            deadline = time.monotonic() + OLLAMA_WAIT_SECONDS
            while probes.ollama_models(spec.get("base_url") or "") is None:
                if time.monotonic() > deadline:
                    _stamp("ai-fallback: `ollama serve` did not answer in time")
                    return 1
                time.sleep(1)
            continue
        if cmd[1:2] == ["pull"] and has_model(probes.ollama_models(spec.get("base_url") or "") or [], spec.get("model") or ""):
            _stamp("ai-fallback: the model is already there")
            continue
        _stamp("ai-fallback: $ " + " ".join(cmd))
        code = subprocess.call(cmd, env=tool_env(docker), cwd=str(ROOT))  # noqa: S603
        if code != 0:
            _stamp(f"ai-fallback: exit status {code}")
            return code
    _stamp("ai-fallback: done")
    return 0


# ============================================================================= entry points
def settings_values() -> tuple[list[str], str | None, str]:
    """(AI_PROVIDER_ORDER, AI_DOCKER_BASE_URL, AI_DOCKER_MODEL) from the app settings (environment + .env)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.config import build_settings

    s = build_settings()
    return list(s.ai_provider_order), s.ai_docker_base_url, s.ai_docker_model


def run(*, dry_run: bool = False, as_json: bool = False, disabled: bool = False, probes: Probes | None = None,
        out: Any = None, values: tuple[list[str], str | None, str] | None = None,
        popen: Callable[..., Any] = subprocess.Popen) -> Decision | None:
    """Decide, start in the background (unless ``dry_run``) and print ONE line. Never raises: the app starts anyway."""
    out = out or sys.stdout
    try:
        order, base_url, model = values if values is not None else settings_values()
    except BaseException as exc:  # noqa: BLE001 - ConfigError (a SystemExit) included: python -m app reports it
        print(f"[ai] local AI fallback: settings could not be read ({exc.__class__.__name__}) - skipped", file=out)
        return None
    decision = decide(order, base_url, model, probes=probes, disabled=disabled)
    if as_json:
        print(json.dumps(decision.as_dict()), file=out)
    elif decision.action != "skip" or dry_run:
        print(f"[ai] {decision.message}", file=out, flush=True)
    if dry_run:
        for cmd in decision.commands:
            print("[ai] would run in the background: " + " ".join(cmd), file=out)
        return decision
    if decision.commands:
        try:
            spawn_background(decision, popen=popen)
        except OSError as exc:
            print(f"[ai] local AI fallback could not be started in the background: {exc}", file=out)
    return decision


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_background"]:
        return background(json.loads(argv[1]))
    ap = argparse.ArgumentParser(prog="ai_fallback.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the decision and the commands; start nothing")
    ap.add_argument("--json", action="store_true", help="print the decision as JSON")
    ap.add_argument("--no-ai-docker", action="store_true", help="skip (the same as not starting it)")
    args = ap.parse_args(argv)
    run(dry_run=args.dry_run, as_json=args.json, disabled=args.no_ai_docker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
