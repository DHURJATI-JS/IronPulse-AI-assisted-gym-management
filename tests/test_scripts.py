"""WP-J: management CLI, diagnostics, database bootstrap, setup/run scripts, Docker files and docs.

Nothing here touches the real .env, the main database or running servers: CLI calls are in-process with
monkeypatched runners or ``--help``/``--dry-run`` subprocesses, .env edits use temporary copies, and database
checks use this package's own TEST_DATABASE_URL (read-only, except the super-admin tests which use the test DB).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import QueryCounter
from app.models import AuditLog, EmailOutbox, Notification, User, UserSession
from app.models.enums import Role, Severity
from app.security.passwords import verify_password
from tests.factories import make_customer, make_user

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
IS_WINDOWS = sys.platform.startswith("win")
SHELL_SCRIPTS = [ROOT / "setup.sh", ROOT / "run.sh", SCRIPTS / "runtime_postgres.sh", SCRIPTS / "runtime_redis.sh"]
PS_SCRIPTS = [ROOT / "setup.ps1", ROOT / "run.ps1", *sorted((SCRIPTS / "windows").glob("*.ps1"))]
STRONG_PASSWORD = "Sturdy-Lift-2048!"


def _load(name: str) -> Any:
    key = f"ironpulse_scripts_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


manage = _load("manage")
check_env = _load("check_env")
init_db = _load("init_db")
make_env = _load("make_env")


def _py(*args: str, cwd: Path = ROOT, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _example_env(tmp_path: Path) -> Path:
    env = tmp_path / ".env"
    shutil.copyfile(ROOT / ".env.example", env)
    return env


def _file_fingerprint(path: Path) -> tuple[int, str] | None:
    if not path.exists():
        return None
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


# ===================================================================================== manage.py CLI
COMMANDS = ["init-db", "reset-db", "seed", "create-superadmin", "build-assets", "gen-videos", "check", "run", "test",
            "stress", "make-env", "configure", "env", "ai-status", "ai-chat"]


def test_manage_help_lists_every_command():
    proc = _py(str(SCRIPTS / "manage.py"), "--help")
    assert proc.returncode == 0, proc.stderr
    for command in COMMANDS:
        assert command in proc.stdout
    assert "sys.executable" in proc.stdout


@pytest.mark.parametrize("command", ["init-db", "check", "ai-chat", "configure"])
def test_manage_subcommand_help_subprocess(command):
    proc = _py(str(SCRIPTS / "manage.py"), command, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout and command in proc.stdout


@pytest.mark.parametrize("command", COMMANDS)
def test_every_subcommand_has_help(command, capsys):
    with pytest.raises(SystemExit) as exc:
        manage.main([command, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: manage.py " + command)


def test_no_command_prints_overview(capsys):
    assert manage.main([]) == 0
    assert "init-db" in capsys.readouterr().out


def test_unknown_arguments_are_rejected_for_regular_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        manage.main(["seed", "--bogus"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_passthrough_commands_forward_arguments_with_this_interpreter(monkeypatch):
    calls: list[tuple[list[str], dict | None]] = []

    def fake_call(cmd, cwd=None, env=None):
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(manage.subprocess, "call", fake_call)
    assert manage.main(["test", "-k", "foo", "tests/test_scripts.py", "-q"]) == 0
    assert manage.main(["build-assets", "--no-download"]) == 0
    assert manage.main(["make-env", "--set", "PORT=12345"]) == 0
    assert manage.main(["stress", "--scenario", "public", "--requests", "5"]) == 0
    assert calls[0][0] == [sys.executable, "-m", "pytest", "-k", "foo", "tests/test_scripts.py", "-q"]
    assert calls[1][0] == [sys.executable, str(SCRIPTS / "build_assets.py"), "--no-download"]
    assert calls[2][0] == [sys.executable, str(SCRIPTS / "make_env.py"), "--set", "PORT=12345"]
    assert calls[3][0] == [sys.executable, str(SCRIPTS / "stress_test.py"), "--scenario", "public", "--requests", "5"]


def test_run_command_variants(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append((list(args), env or {})) or 0)
    assert manage.main(["run", "--reload", "--port", "12345"]) == 0
    args, env = calls[-1]
    assert args == ["-m", "app"] and env["RELOAD"] == "true" and env["PORT"] == "12345"

    assert manage.main(["run", "--workers", "3", "--host", "127.0.0.1", "--port", "12346"]) == 0
    args, env = calls[-1]
    assert args[:3] == ["-m", "uvicorn", "app.main:app"]
    assert args[args.index("--workers") + 1] == "3" and args[args.index("--port") + 1] == "12346"
    assert env["REALTIME_PG_BRIDGE"] == "true"

    assert manage.main(["run", "--reload", "--workers", "2"]) == 2


def test_reset_db_requires_confirmation_and_refuses_production(monkeypatch, capsys):
    monkeypatch.setattr(manage, "load_settings", lambda: pytest.fail("settings must not even be loaded without --yes"))
    assert manage.main(["reset-db"]) == 2
    assert "--yes" in capsys.readouterr().out
    monkeypatch.setattr(manage, "load_settings", lambda: SimpleNamespace(is_production=True, database_url="postgresql+asyncpg://u:p@h/db"))
    assert manage.main(["reset-db", "--yes"]) == 2  # refused before anything is dropped
    assert "production" in capsys.readouterr().err


def test_seed_is_wired_to_seed_demo(monkeypatch):
    ok, why = manage.seed_available()
    assert ok, why
    calls: list[list[str]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append(list(args)) or 0)
    assert manage.main(["seed"]) == 0
    assert calls == [[str(SCRIPTS / "seed_demo.py")]]


def test_seed_and_generators_explain_when_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(manage, "SCRIPTS", tmp_path)
    monkeypatch.setattr(manage, "run_python", lambda *a, **k: pytest.fail("must not run anything"))
    assert manage.main(["seed"]) == 1
    assert "not available yet" in capsys.readouterr().out
    assert manage.main(["gen-videos"]) == 1
    assert "generate_videos.py" in capsys.readouterr().out
    assert manage.main(["stress"]) == 1
    assert "stress_test.py" in capsys.readouterr().out


def test_gen_videos_runs_the_generator_when_present(monkeypatch, tmp_path):
    (tmp_path / "generate_videos.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(manage, "SCRIPTS", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append(list(args)) or 0)
    assert manage.main(["gen-videos", "--only", "squat"]) == 0
    assert calls == [[str(tmp_path / "generate_videos.py"), "--only", "squat"]]


# ===================================================================================== configure / env
def test_configure_docker_mode_is_idempotent(tmp_path):
    env = _example_env(tmp_path)
    changed = manage.configure_env(env, "docker")
    values = make_env.parse(env.read_text(encoding="utf-8"))
    for key in ("POSTGRES_PASSWORD", "APP_DB_PASSWORD", "REDIS_PASSWORD"):
        assert len(values[key]) >= 24
    assert values["IRONPULSE_DB_MODE"] == "docker"
    assert values["DATABASE_URL"] == f"postgresql+asyncpg://ironpulse:{values['APP_DB_PASSWORD']}@localhost:5433/ironpulse"
    assert values["TEST_DATABASE_URL"].endswith("@localhost:5433/ironpulse_test")
    assert values["DATABASE_ADMIN_URL"] == f"postgresql+asyncpg://postgres:{values['POSTGRES_PASSWORD']}@localhost:5433/postgres"
    assert values["REDIS_URL"] == f"redis://:{values['REDIS_PASSWORD']}@localhost:6380/0"
    assert values["AI_DOCKER_BASE_URL"] == "http://localhost:11434/v1" and values["AI_DOCKER_MODEL"] == "gemma3:4b"
    assert values["TTS_BASE_URL"] == "http://localhost:8880/v1"
    assert "DATABASE_URL" in changed
    before = env.read_bytes()
    assert manage.configure_env(env, "docker") == {}  # second run: nothing changes, passwords are kept
    assert env.read_bytes() == before
    assert "# optional superuser URL" in env.read_text(encoding="utf-8")  # inline comments survive
    if not IS_WINDOWS:
        assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_configure_runtime_mode_uses_password_files(tmp_path):
    env = _example_env(tmp_path)
    home = tmp_path / "runtime"
    manage.configure_env(env, "runtime", runtime_home=home, tesseract="/opt/tess/bin/tesseract")
    values = make_env.parse(env.read_text(encoding="utf-8"))
    app_pw = (home / "app_db_password").read_text(encoding="utf-8")
    redis_pw = (home / "redis_password").read_text(encoding="utf-8")
    assert (home / "pg_superuser_password").read_text(encoding="utf-8")
    assert values["DATABASE_URL"] == f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:5432/ironpulse"
    assert values["REDIS_URL"] == f"redis://:{redis_pw}@127.0.0.1:6379/0"
    assert values["DATABASE_ADMIN_URL"] == ""  # init-db discovers the runtime superuser itself
    assert values["TESSERACT_CMD"] == "/opt/tess/bin/tesseract"
    if not IS_WINDOWS:
        for name in ("app_db_password", "redis_password", "pg_superuser_password"):
            assert stat.S_IMODE((home / name).stat().st_mode) == 0o600
    assert manage.configure_env(env, "runtime", runtime_home=home, tesseract="/opt/tess/bin/tesseract") == {}


def test_configure_existing_and_native_modes(tmp_path):
    env = _example_env(tmp_path)
    manage.configure_env(env, "existing", database_url="postgres://me:pw@db.example.org:6543/gymdb", redis="none")
    values = make_env.parse(env.read_text(encoding="utf-8"))
    assert values["DATABASE_URL"] == "postgresql+asyncpg://me:pw@db.example.org:6543/gymdb"
    assert values["TEST_DATABASE_URL"] == "postgresql+asyncpg://me:pw@db.example.org:6543/gymdb_test"
    assert values["REDIS_URL"] == ""
    manage.configure_env(env, "native", admin_url="postgresql://postgres:s3cret@localhost:5432/postgres")
    values = make_env.parse(env.read_text(encoding="utf-8"))
    assert values["DATABASE_URL"].startswith("postgresql+asyncpg://ironpulse:") and values["DATABASE_URL"].endswith("@localhost:5432/ironpulse")
    assert values["DATABASE_ADMIN_URL"] == "postgresql+asyncpg://postgres:s3cret@localhost:5432/postgres"
    assert values["IRONPULSE_DB_MODE"] == "native"


def test_configure_validation_errors(tmp_path):
    with pytest.raises(manage.CommandError):
        manage.configure_env(tmp_path / "missing.env", "docker")
    env = _example_env(tmp_path)
    with pytest.raises(manage.CommandError):
        manage.configure_env(env, "cloud")
    with pytest.raises(manage.CommandError):
        manage.configure_env(env, "existing", database_url="postgresql://u:p@host:5432")  # no database name


def test_configure_command_prints_redacted_values(tmp_path, capsys):
    env = _example_env(tmp_path)
    assert manage.main(["configure", "--db", "docker", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    values = make_env.parse(env.read_text(encoding="utf-8"))
    assert "DATABASE_URL = postgresql+asyncpg://ironpulse:***@localhost:5433/ironpulse" in out
    assert values["APP_DB_PASSWORD"] not in out and values["POSTGRES_PASSWORD"] not in out
    assert manage.main(["configure", "--db", "docker", "--env-file", str(env)]) == 0
    assert "nothing changed" in capsys.readouterr().out


def test_env_get_ensure_set(tmp_path, capsys, monkeypatch):
    env = _example_env(tmp_path)
    assert manage.main(["env", "get", "PORT", "APP_NAME", "NOPE", "--env-file", str(env)]) == 0
    assert capsys.readouterr().out.splitlines() == ["10000", "IronPulse", ""]
    assert manage.main(["env", "ensure", "REDIS_PASSWORD", "APP_NAME", "--env-file", str(env)]) == 0
    values = make_env.parse(env.read_text(encoding="utf-8"))
    assert len(values["REDIS_PASSWORD"]) >= 24 and values["APP_NAME"] == "IronPulse"
    first = values["REDIS_PASSWORD"]
    manage.main(["env", "ensure", "REDIS_PASSWORD", "--env-file", str(env)])
    assert make_env.parse(env.read_text(encoding="utf-8"))["REDIS_PASSWORD"] == first
    assert manage.main(["env", "set", "PORT=12345", "--env-file", str(env)]) == 0
    assert make_env.parse(env.read_text(encoding="utf-8"))["PORT"] == "12345"
    assert manage.main(["env", "set", "lower=1", "--env-file", str(env)]) == 2
    capsys.readouterr()
    monkeypatch.setenv("PORT", "15555")
    manage.main(["env", "get", "--effective", "PORT", "--env-file", str(env)])
    assert capsys.readouterr().out.strip() == "15555"


def test_make_env_is_idempotent_on_a_copy_of_env_example(tmp_path):
    real_env = ROOT / ".env"
    fingerprint = _file_fingerprint(real_env)
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(SCRIPTS / "make_env.py", tmp_path / "scripts" / "make_env.py")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    script = str(tmp_path / "scripts" / "make_env.py")

    first = _py(script, "--quiet", cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    env = tmp_path / ".env"
    values = make_env.parse(env.read_text(encoding="utf-8"))
    assert values["SECRET_KEY"] not in make_env.PLACEHOLDERS and len(values["SECRET_KEY"]) >= 48
    assert len(values["ID_HASH_PEPPER"]) >= 16 and values["SUPERADMIN_PASSWORD"]
    from cryptography.fernet import Fernet

    Fernet(values["DATA_ENCRYPTION_KEY"].encode())  # a valid Fernet key
    snapshot = env.read_bytes()
    assert _py(script, "--quiet", cwd=tmp_path).returncode == 0
    assert env.read_bytes() == snapshot  # second run changes nothing
    assert _py(script, "--quiet", "--set", "PORT=12345", cwd=tmp_path).returncode == 0
    after = make_env.parse(env.read_text(encoding="utf-8"))
    assert after["PORT"] == "12345" and after["SECRET_KEY"] == values["SECRET_KEY"]
    if not IS_WINDOWS:
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert _file_fingerprint(real_env) == fingerprint  # the project's real .env was never touched


# ===================================================================================== check_env
def test_check_env_main_returns_structured_results(capsys):
    report = check_env.main(["--no-network", "--quiet"])
    assert isinstance(report, check_env.Report)
    assert capsys.readouterr().out == ""
    names = {r.name for r in report.results}
    for expected in ("Python version", "Virtual environment", "Python packages", ".env file", "Required secrets",
                     "Settings load", "PostgreSQL connection", "Extensions", "pgvector", "Tesseract", "OCR engines",
                     "Tailwind CLI", "Built CSS/JS", "ffmpeg (imageio-ffmpeg)", "Generated videos", "Gemma gateway",
                     "Gemma in Docker", "Claude (Anthropic)", "Kokoro TTS", "E-mail (SMTP)", "Maps (Geoapify)",
                     "Payments (Cashfree)", "App port", "Redis sessions"):
        assert expected in names, expected
    for r in report.results:
        assert r.status in check_env.STATUSES and r.group in check_env.GROUP_ORDER and r.detail is not None
    # this package's own test database is reachable and has every extension
    assert report.by_name("PostgreSQL connection").status == check_env.OK
    assert report.by_name("Extensions").status == check_env.OK
    assert report.by_name("pgvector").detail.startswith("version ")
    assert report.by_name("Gemma gateway").status == check_env.SKIP  # --no-network
    assert report.exit_code in (0, 1)
    json.dumps(report.as_dict())


def test_check_env_json_and_table_output(capsys):
    report = check_env.main(["--no-network", "--no-db", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"results", "counts", "exit_code"}
    assert data["exit_code"] == report.exit_code
    assert report.by_name("PostgreSQL connection").status == check_env.SKIP
    text = check_env.render(report)
    assert "IronPulse environment check" in text and "Summary:" in text and "[ OK ]" in text


def test_manage_check_returns_the_exit_code(capsys):
    code = manage.main(["check", "--no-network", "--no-db", "--quiet"])
    assert code in (0, 1)
    assert capsys.readouterr().out == ""


def test_check_python_and_venv():
    assert check_env.check_python((3, 9, 7)).status == check_env.FAIL
    assert check_env.check_python((3, 9, 7)).critical
    assert check_env.check_python((3, 12, 1)).status == check_env.OK
    assert check_env.check_venv("/x/.venv", "/usr").status == check_env.OK
    assert check_env.check_venv("/usr", "/usr").status == check_env.WARN


def _raw_ctx(tmp_path: Path, lines: list[str], **kwargs: Any) -> Any:
    env = tmp_path / "check.env"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return check_env.Ctx(env_path=env, load_app_settings=False, **kwargs)


def test_check_secrets_detects_placeholders(tmp_path, monkeypatch):
    for key in ("SECRET_KEY", "DATA_ENCRYPTION_KEY", "ID_HASH_PEPPER", "SUPERADMIN_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    bad = _raw_ctx(tmp_path, ["SECRET_KEY=change-me-run-setup-to-generate-a-64-char-secret", "DATA_ENCRYPTION_KEY=", "ID_HASH_PEPPER=short"])
    results = check_env.check_secrets(bad)
    assert results[0].status == check_env.FAIL and results[0].critical
    assert "SECRET_KEY" in results[0].detail and "DATA_ENCRYPTION_KEY" in results[0].detail
    assert any(r.name == "Super-admin password" and r.status == check_env.WARN for r in results)
    import base64

    good = _raw_ctx(tmp_path, ["SECRET_KEY=" + "k" * 20 + "Q9x" * 10, "DATA_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode(),
                               "ID_HASH_PEPPER=" + "p" * 24, "SUPERADMIN_PASSWORD=Xy7-strong-pass"])
    assert [r.status for r in check_env.check_secrets(good)] == [check_env.OK]


def test_report_exit_code_rules():
    R = check_env.Result
    assert check_env.Report([R("System", "a", check_env.FAIL, critical=True)]).exit_code == 1
    assert check_env.Report([R("System", "a", check_env.FAIL), R("System", "b", check_env.WARN)]).exit_code == 0
    assert check_env.Report([R("System", "b", check_env.WARN)], strict=True).exit_code == 1
    assert check_env.Report([R("System", "b", check_env.INFO), R("System", "c", check_env.OK)], strict=True).exit_code == 0


GATEWAY_MODELS = {"object": "list", "models": [{"name": "gemma", "model": "gemma"}],
                  "data": [{"id": "gemma", "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx": 8192}}]}
GATEWAY_STATUS = {"this_pc_cpu_percent": 2.8, "backends": [
    {"name": "this-pc", "url": "http://127.0.0.1:8080", "online": True, "busy_slots": "0/2", "overloaded": False},
    {"name": "other-pc", "url": "http://192.168.0.102:8080", "online": True, "busy_slots": "1/2", "overloaded": False}]}


def _fake_http(routes: dict[str, tuple[int | None, Any, str | None]], seen: list | None = None):
    def fake(url, headers=None, timeout=3.0):
        if seen is not None:
            seen.append((url, dict(headers or {})))
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                return answer
        return None, None, "URLError: [Errno 61] Connection refused"
    return fake


AI_ENV = ["AI_GATEWAY_BASE_URL=http://192.168.0.101:8000/v1", "AI_GATEWAY_API_KEY=test-gateway-key", "AI_GATEWAY_MODEL=gemma",
          "AI_GATEWAY_STATUS_URL=http://192.168.0.101:8000/status", "AI_DOCKER_BASE_URL=http://localhost:11434/v1",
          "AI_DOCKER_MODEL=gemma3:4b", "TTS_BASE_URL=http://localhost:8880/v1", "TTS_VOICE=af_heart"]


@pytest.fixture
def clean_ai_env(monkeypatch):
    for key in ("AI_GATEWAY_BASE_URL", "AI_GATEWAY_API_KEY", "AI_GATEWAY_MODEL", "AI_GATEWAY_STATUS_URL", "AI_DOCKER_BASE_URL",
                "AI_DOCKER_MODEL", "AI_DOCKER_API_KEY", "ANTHROPIC_API_KEY", "TTS_BASE_URL", "TTS_VOICE"):
        monkeypatch.delenv(key, raising=False)


def test_gemma_gateway_models_and_status_page(tmp_path, monkeypatch, clean_ai_env):
    seen: list = []
    monkeypatch.setattr(check_env, "http_get", _fake_http({
        "http://192.168.0.101:8000/v1/models": (200, GATEWAY_MODELS, None),
        "http://192.168.0.101:8000/status": (200, GATEWAY_STATUS, None),
    }, seen))
    results = {r.name: r for r in check_env.check_ai(_raw_ctx(tmp_path, AI_ENV))}
    assert results["Gemma gateway"].status == check_env.OK and "gemma" in results["Gemma gateway"].detail
    assert results["Gateway status page"].status == check_env.OK
    assert "2/2 backends online" in results["Gateway status page"].detail and "other-pc up 1/2" in results["Gateway status page"].detail
    assert results["Gemma in Docker"].status == check_env.INFO  # failsafe not running is fine
    assert results["AI failover chain"].status == check_env.OK and "answering now: gateway" in results["AI failover chain"].detail
    assert ("http://192.168.0.101:8000/v1/models", {"Authorization": "Bearer test-gateway-key"}) in seen


def test_ai_all_down_and_model_problems(tmp_path, monkeypatch, clean_ai_env):
    monkeypatch.setattr(check_env, "http_get", _fake_http({}))
    results = {r.name: r for r in check_env.check_ai(_raw_ctx(tmp_path, AI_ENV))}
    assert results["Gemma gateway"].status == check_env.WARN and "unreachable" in results["Gemma gateway"].detail
    assert results["AI failover chain"].status == check_env.WARN and "AI unavailable" in results["AI failover chain"].detail
    assert "ollama-pull" in results["AI failover chain"].fix

    monkeypatch.setattr(check_env, "http_get", _fake_http({
        "http://192.168.0.101:8000/v1/models": (401, None, "HTTP 401"),
        "http://localhost:11434/v1/models": (200, {"data": [{"id": "llama3:8b"}]}, None),
    }))
    results = {r.name: r for r in check_env.check_ai(_raw_ctx(tmp_path, AI_ENV + ["ANTHROPIC_API_KEY=sk-test"]))}
    assert "AI_GATEWAY_API_KEY" in results["Gemma gateway"].fix
    assert results["Gemma in Docker"].status == check_env.WARN and "not pulled" in results["Gemma in Docker"].detail
    assert results["Claude (Anthropic)"].status == check_env.OK
    assert "answering now: anthropic" in results["AI failover chain"].detail

    monkeypatch.setattr(check_env, "http_get", _fake_http({
        "http://192.168.0.101:8000/v1/models": (200, {"data": [{"id": "llama"}]}, None),
        "http://localhost:11434/v1/models": (200, {"data": [{"id": "gemma3:4b"}]}, None),
    }))
    results = {r.name: r for r in check_env.check_ai(_raw_ctx(tmp_path, AI_ENV))}
    assert results["Gemma gateway"].status == check_env.WARN and "AI_GATEWAY_MODEL=gemma" in results["Gemma gateway"].fix
    assert results["Gemma in Docker"].status == check_env.OK


def test_kokoro_probe(tmp_path, monkeypatch, clean_ai_env):
    ctx = _raw_ctx(tmp_path, AI_ENV)
    monkeypatch.setattr(check_env, "http_get", _fake_http({"http://localhost:8880/v1/audio/voices": (200, {"voices": ["af_heart", "bf_emma"]}, None)}))
    assert check_env.check_kokoro(ctx).status == check_env.OK
    monkeypatch.setattr(check_env, "http_get", _fake_http({"http://localhost:8880/v1/audio/voices": (200, {"voices": ["bf_emma"]}, None)}))
    assert check_env.check_kokoro(ctx).status == check_env.WARN
    monkeypatch.setattr(check_env, "http_get", _fake_http({}))
    down = check_env.check_kokoro(ctx)
    assert down.status == check_env.INFO and "browser voice" in down.detail and "--profile voice" in down.fix
    assert check_env.check_kokoro(_raw_ctx(tmp_path, AI_ENV, network=False)).status == check_env.SKIP


def test_port_check_detects_a_busy_port(tmp_path, monkeypatch):
    for key in ("HOST", "PORT"):
        monkeypatch.delenv(key, raising=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        result = check_env.check_port(_raw_ctx(tmp_path, ["HOST=127.0.0.1", f"PORT={port}"], network=False))
        assert result.status == check_env.WARN and "in use" in result.detail
    free = check_env.check_port(_raw_ctx(tmp_path, ["HOST=127.0.0.1", f"PORT={port}"], network=False))
    assert free.status == check_env.OK
    assert check_env.check_port(_raw_ctx(tmp_path, ["PORT=abc"], network=False)).status == check_env.FAIL


def test_ocr_engine_check_handles_stub_and_real_service(monkeypatch):
    from app.services import ocr

    ctx = check_env.Ctx(network=False, db=False)

    def not_ready():
        raise NotImplementedError

    monkeypatch.setattr(ocr, "available_engines", not_ready)
    assert check_env.check_ocr_engines(ctx).status == check_env.WARN
    monkeypatch.setattr(ocr, "available_engines", lambda: ["tesseract", "apple_vision"])
    assert check_env.check_ocr_engines(ctx).status == check_env.OK
    monkeypatch.setattr(ocr, "available_engines", lambda: ["tesseract"])
    assert check_env.check_ocr_engines(ctx).status == check_env.WARN


def test_asset_and_video_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(check_env, "ROOT", tmp_path)
    missing = check_env.check_dist_assets()
    assert missing.status == check_env.FAIL and missing.critical
    dist = tmp_path / "app" / "static" / "dist"
    dist.mkdir(parents=True)
    (dist / "app.min.css").write_text("body{}", encoding="utf-8")
    (dist / "app.min.js").write_text("1;", encoding="utf-8")
    assert check_env.check_dist_assets().status == check_env.OK
    assert check_env.check_videos().status == check_env.WARN
    videos = tmp_path / "app" / "static" / "videos"
    videos.mkdir(parents=True)
    (videos / "squat.mp4").write_bytes(b"\x00" * 1024)
    assert check_env.check_videos().status == check_env.OK


def test_ai_status_command_uses_the_probes(monkeypatch, capsys):
    # the configured gateway (test settings) answers through the fake transport - its model list and the 1-token
    # latency probe (POST /chat/completions)
    s = get_settings()
    monkeypatch.setattr(check_env, "http_get", _fake_http({
        f"{s.ai_gateway_base_url.rstrip('/')}/models": (200, GATEWAY_MODELS, None),
    }))
    posted: list[str] = []
    monkeypatch.setattr(check_env, "http_post_json", lambda url, payload, headers=None, timeout=60.0: posted.append(
        url) or (200, {"choices": [{"message": {"content": "OK"}}]}, None))
    assert manage.main(["ai-status"]) == 0
    out = capsys.readouterr().out
    assert "Gemma gateway" in out and "1-token answer in" in out
    assert posted == [f"{s.ai_gateway_base_url.rstrip('/')}/chat/completions"]
    assert manage.main(["ai-status", "--no-probe"]) == 0 and len(posted) == 1
    monkeypatch.setattr(check_env, "http_get", _fake_http({}))
    code = manage.main(["ai-status"])
    assert code == (0 if s.anthropic_api_key else 1)


# ===================================================================================== init_db
def test_url_helpers():
    assert init_db.normalize_url("postgres://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert init_db.normalize_url("postgresql://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert init_db.normalize_url("postgresql+asyncpg://u:p@h/d") == "postgresql+asyncpg://u:p@h/d"
    assert "secret" not in init_db.redact("postgresql+asyncpg://u:secret@h/d") and "***" in init_db.redact("postgresql://u:secret@h/d")
    assert init_db.with_database("postgresql+asyncpg://u:p%40x@h:5433/app", "postgres") == "postgresql+asyncpg://u:p%40x@h:5433/postgres"
    dsn, kwargs = init_db.asyncpg_connect_args("postgresql+asyncpg://u:p@h:5432/d?ssl=require")
    assert dsn == "postgresql://u:p@h:5432/d" and kwargs == {"ssl": "require"}


def test_identifier_quoting_rejects_injection():
    assert init_db.quote_ident("ironpulse_test") == '"ironpulse_test"'
    for bad in ('x"; DROP DATABASE ironpulse; --', "", "a b", "x" * 64):
        with pytest.raises(ValueError):
            init_db.quote_ident(bad)
    assert init_db.quote_literal("it's") == "'it''s'"


def test_pgvector_hints_are_os_specific():
    assert "install_pgvector.ps1" in init_db.pgvector_install_hint("win32", 17) and "Docker" in init_db.pgvector_install_hint("win32")
    assert "brew install pgvector" in init_db.pgvector_install_hint("darwin")
    assert "postgresql-16-pgvector" in init_db.pgvector_install_hint("linux", 16)
    assert "DATABASE_ADMIN_URL" in init_db.superuser_hint()


def test_runtime_admin_discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("IRONPULSE_PGPORT", raising=False)
    (tmp_path / "pg_superuser_password").write_text("RuntimeSuper123\n", encoding="utf-8")
    url = init_db.runtime_admin_url("postgresql+asyncpg://ironpulse:x@localhost:5432/ironpulse", tmp_path)
    assert url == "postgresql+asyncpg://postgres:RuntimeSuper123@localhost:5432/postgres"
    assert init_db.runtime_admin_url("postgresql+asyncpg://ironpulse:x@db.example:5432/ironpulse", tmp_path) is None
    assert init_db.runtime_admin_url("postgresql+asyncpg://ironpulse:x@localhost:5433/ironpulse", tmp_path) is None
    assert init_db.runtime_admin_url("postgresql+asyncpg://ironpulse:x@localhost:5432/ironpulse", tmp_path / "nope") is None


def test_connect_error_descriptions():
    url = "postgresql+asyncpg://ironpulse:x@localhost:5432/ironpulse"

    class PgErr(Exception):
        def __init__(self, state: str, msg: str = "boom") -> None:
            super().__init__(msg)
            self.sqlstate = state

    assert "password authentication failed" in init_db.describe_connect_error(PgErr("28P01"), url)[0]
    assert "does not exist" in init_db.describe_connect_error(PgErr("3D000"), url)[0]
    detail, hint = init_db.describe_connect_error(ConnectionRefusedError(61, "refused"), url)
    assert "cannot reach PostgreSQL at localhost:5432" in detail and "runtime_postgres.sh" in hint or "Start-Service" in hint
    assert "timed out" in init_db.describe_connect_error(TimeoutError(), url)[0]


def _test_db_url() -> str:
    return os.environ["DATABASE_URL"]


async def test_init_db_check_mode_reports_the_existing_test_database():
    url = _test_db_url()
    report = await init_db.init_database(url, check_only=True, create_tables=False, log=lambda _m: None)
    dbname = init_db.parse_url(url).database
    assert report.ok, report.steps
    assert report.get(f"Database {dbname}").status == init_db.OK
    assert report.get("App login").status == init_db.OK
    for ext in init_db.EXTENSIONS:
        assert report.get(f"Extension {ext} ({dbname})").status == init_db.OK


async def test_init_db_check_mode_never_creates_a_missing_database(db):
    url = init_db.with_database(_test_db_url(), init_db.parse_url(_test_db_url()).database + "_wpj_missing")
    report = await init_db.init_database(url, check_only=True, create_tables=False, log=lambda _m: None)
    name = init_db.parse_url(url).database
    assert report.get(f"Database {name}").status == init_db.WARN
    from sqlalchemy import text

    assert (await db.execute(text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": name})).scalar() == 0


async def test_init_db_unreachable_server_and_bad_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    report = await init_db.init_database(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port}/ironpulse_x", create_tables=False)
    step = report.get("PostgreSQL server")
    assert not report.ok and step.status == init_db.FAIL and "cannot reach" in step.detail and step.hint
    bad = await init_db.init_database("postgresql+asyncpg://localhost/", create_tables=False)
    assert bad.get("Database URL").status == init_db.FAIL


def test_init_db_cli_check_mode(capsys):
    assert init_db.main(["--check", "--database-url", _test_db_url()]) == 0
    out = capsys.readouterr().out
    assert "check finished" in out and "***" in out and "Extension vector" in out


# ===================================================================================== create-superadmin
async def test_create_superadmin_creates_account_with_notification_and_audit(db):
    result = await manage.create_superadmin(db, email="Ops.Admin@Example.com", password=STRONG_PASSWORD, name="Ops Admin")
    await db.commit()
    assert result["action"] == "created"
    user = await db.get(User, result["user_id"])
    assert user.role == Role.SUPERADMIN and user.email == "ops.admin@example.com" and user.email_verified_at is not None
    assert user.username and await verify_password(user.password_hash, STRONG_PASSWORD)
    note = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert note is not None and note.link == "/superadmin"
    assert await db.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.user_id == user.id)) == 1
    audit_row = await db.scalar(select(AuditLog).where(AuditLog.entity_id == user.id))
    assert audit_row.action == "superadmin.created" and audit_row.detail["via"] == "cli"


async def test_create_superadmin_validation(db):
    with pytest.raises(manage.CommandError) as weak:
        await manage.create_superadmin(db, email="sec@example.com", password="short")
    assert weak.value.code == 2
    with pytest.raises(manage.CommandError):
        await manage.create_superadmin(db, email="not-an-email", password=STRONG_PASSWORD)
    member = await make_customer(db, email="member@example.com")
    with pytest.raises(manage.CommandError) as taken:
        await manage.create_superadmin(db, email="member@example.com", password=STRONG_PASSWORD)
    assert "customer" in str(taken.value)
    assert (await db.get(User, member.id)).role == Role.CUSTOMER


async def test_create_superadmin_reset_password_revokes_sessions(db):
    admin = await make_user(db, role=Role.SUPERADMIN, email="root@example.com", full_name="Root Admin")
    from datetime import timedelta

    from app.core.utils import utcnow

    db.add(UserSession(id="h" * 64, user_id=admin.id, expires_at=utcnow() + timedelta(hours=1), ip="127.0.0.1", user_agent="pytest"))
    await db.commit()
    with pytest.raises(manage.CommandError) as exists:
        await manage.create_superadmin(db, email="root@example.com", password=STRONG_PASSWORD)
    assert "--reset-password" in str(exists.value)
    result = await manage.create_superadmin(db, email="root@example.com", password=STRONG_PASSWORD, reset=True)
    await db.commit()
    assert result["action"] == "reset"
    admin_id = admin.id
    db.expire_all()
    fresh = await db.get(User, admin_id)
    assert await verify_password(fresh.password_hash, STRONG_PASSWORD)
    session = await db.get(UserSession, "h" * 64)
    assert session.revoked_at is not None
    note = await db.scalar(select(Notification).where(Notification.user_id == admin_id))
    assert note.severity == Severity.WARNING
    actions = (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == admin_id))).scalars().all()
    assert "superadmin.password_reset" in actions


async def test_create_superadmin_query_count_does_not_grow_with_users(db):
    async def count_for(email: str, name: str) -> int:
        with QueryCounter() as qc:
            await manage.create_superadmin(db, email=email, password=STRONG_PASSWORD, name=name)
        await db.commit()
        return qc.count

    for i in range(2):
        await make_customer(db, email=f"few{i}@example.com")
    few = await count_for("admin.one@example.com", "Night Shift")
    for i in range(25):
        await make_customer(db, email=f"many{i}@example.com")
    many = await count_for("admin.two@example.com", "Day Shift")
    assert few == many, (few, many)


def test_generated_passwords_pass_the_policy():
    from app.security.passwords import password_problems

    for _ in range(20):
        pw = manage.generate_password()
        assert len(pw) == 20 and not password_problems(pw)


# ===================================================================================== ai-chat (OpenAI SDK -> Gemma gateway)
class _FakeCompletions:
    def __init__(self, pieces: list[str], error: Exception | None = None) -> None:
        self.pieces, self.error, self.calls = pieces, error, []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if kwargs.get("stream"):
            chunk = lambda text: SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])  # noqa: E731
            return iter([chunk(p) for p in self.pieces] + [SimpleNamespace(choices=[])])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="".join(self.pieces)))])


def _fake_client(pieces: list[str], error: Exception | None = None) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(pieces, error)))


def test_chat_once_streams_and_returns_the_answer():
    written: list[str] = []
    client = _fake_client(["Warm ", "up ", "first."])
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "why warm up?"}]
    text = manage.chat_once(client, "gemma", messages, max_tokens=80, write=written.append)
    assert text == "Warm up first." and written[:3] == ["Warm ", "up ", "first."] and written[-1] == "\n"
    call = client.chat.completions.calls[0]
    assert call["model"] == "gemma" and call["stream"] is True and call["max_tokens"] == 80
    assert manage.chat_once(client, "gemma", messages, stream=False, write=written.append) == "Warm up first."


def test_trim_history_keeps_system_prompt_and_recent_turns():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(30):
        messages += [{"role": "user", "content": f"q{i} " + "x" * 900}, {"role": "assistant", "content": f"a{i} " + "y" * 900}]
    trimmed = manage.trim_history(messages)
    assert trimmed[0]["content"] == "sys" and trimmed[1]["role"] == "user"
    assert sum(len(m["content"]) for m in trimmed[1:]) <= 12000
    assert trimmed[-1]["content"].startswith("a29")


def test_ai_target_uses_gateway_settings_and_overrides():
    s = SimpleNamespace(ai_gateway_base_url="http://192.168.0.101:8000/v1", ai_gateway_api_key="k", ai_gateway_model="gemma",
                        ai_docker_base_url="http://localhost:11434/v1", ai_docker_api_key="ollama", ai_docker_model="gemma3:4b")
    assert manage.ai_target(s, "gateway") == {"label": "Gemma gateway", "base_url": "http://192.168.0.101:8000/v1", "api_key": "k", "model": "gemma"}
    assert manage.ai_target(s, "docker")["model"] == "gemma3:4b"
    assert manage.ai_target(s, "gateway", base_url="http://x/v1", model="m")["base_url"] == "http://x/v1"


def test_ai_chat_command_one_shot_and_unreachable_gateway(monkeypatch, capsys):
    import openai

    created: list[dict] = []

    def fake_openai(**kwargs: Any) -> Any:
        created.append(kwargs)
        return _fake_client(["Hello ", "from Gemma"])

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    assert manage.main(["ai-chat", "--max-tokens", "40", "Say", "hello"]) == 0
    assert "Hello from Gemma" in capsys.readouterr().out
    s = get_settings()
    assert created[0]["base_url"] == s.ai_gateway_base_url and created[0]["max_retries"] == 0

    error = openai.APIConnectionError(request=httpx.Request("POST", "http://192.168.0.101:8000/v1/chat/completions"))
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: _fake_client([], error))
    assert manage.main(["ai-chat", "hello"]) == 1
    err = capsys.readouterr().err
    assert "cannot reach" in err and "ai-status" in err


# ===================================================================================== shell scripts
@pytest.mark.skipif(IS_WINDOWS or shutil.which("bash") is None, reason="bash scripts are for macOS / Linux")
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse_and_are_executable(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.access(script, os.X_OK), f"{script.name} is not executable (chmod +x)"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -" in text and "pipefail" in text
    assert "\r\n" not in text


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_avoid_bash4_only_syntax(script):
    code = "\n".join(line for line in script.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
    for pattern in (r"declare -A", r"\$\{\w+,,\}", r"\$\{\w+\^\^\}", r"\bmapfile\b", r"\breadarray\b", r"\|&", r";;&", r"&>>", r"\[\[ -v "):
        assert not re.search(pattern, code), f"{script.name}: bash 4+ construct {pattern!r} (macOS ships bash 3.2)"


@pytest.mark.skipif(IS_WINDOWS or shutil.which("bash") is None, reason="bash scripts are for macOS / Linux")
def test_shell_script_help_dry_run_and_validation():
    def sh(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NO_COLOR": "1"})

    proc = sh("setup.sh", "--help")
    assert proc.returncode == 0 and "--db runtime" in proc.stdout and "--with-ai-docker" in proc.stdout and "--skip-pip" in proc.stdout
    plan = sh("setup.sh", "--dry-run", "--db", "docker", "--with-ai-docker", "--with-voice")
    assert plan.returncode == 0, plan.stderr
    assert "docker compose up -d db redis" in plan.stdout and "ollama" in plan.stdout and "kokoro" in plan.stdout
    runtime = sh("setup.sh", "--dry-run", "--db", "runtime", "--skip-seed")
    assert "micromamba" in runtime.stdout and "pgvector" in runtime.stdout and "manage.py seed" not in runtime.stdout
    existing = sh("setup.sh", "--dry-run", "--db", "existing", "--database-url", "postgresql://me:topsecret@db.example:5432/x")
    assert existing.returncode == 0 and "topsecret" not in existing.stdout and "me:***@db.example" in existing.stdout
    assert sh("setup.sh", "--db", "cloud").returncode == 1
    assert sh("setup.sh", "--db").returncode == 1
    assert sh("run.sh", "--help").returncode == 0
    assert sh("run.sh", "--workers", "x").returncode == 1
    assert "cannot be combined" in sh("run.sh", "--reload", "--workers", "2").stderr
    for script in ("scripts/runtime_postgres.sh", "scripts/runtime_redis.sh"):
        assert sh(script, "--help").returncode == 0
        assert sh(script, "bogus").returncode == 1


def test_shell_scripts_use_the_documented_runtime_layout():
    pg = (SCRIPTS / "runtime_postgres.sh").read_text(encoding="utf-8")
    assert "$HOME/.ironpulse" in pg and "pgdata" in pg and "PG_PORT=\"${IRONPULSE_PGPORT:-5432}\"" in pg
    assert "listen_addresses=localhost" in pg and "SOCKET_DIR=\"${IRONPULSE_PGSOCKET:-/tmp}\"" in pg
    for word in ("start)", "stop)", "status)", "psql)"):
        assert word in pg
    redis = (SCRIPTS / "runtime_redis.sh").read_text(encoding="utf-8")
    assert "redis_password" in redis and "REDISCLI_AUTH" in redis and "6379" in redis and "appendonly yes" in redis
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    for manager in ("brew", "apt-get", "dnf", "pacman", "zypper"):
        assert manager in setup
    for step in ("make_env.py", "configure --db", "init-db --with-test-db", "build-assets", "gen-videos", " seed"):
        assert step in setup
    assert "postgresql@17 pgvector" in setup and "postgresql-$major-pgvector" in setup


# ===================================================================================== PowerShell (cannot run here: static checks)
def _ps_strip(source: str) -> str:
    """Blank out comments and string literals (keeps the code structure for bracket/operator checks)."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if source.startswith("<#", i):
            j = source.find("#>", i + 2)
            assert j != -1, "unterminated <# block comment"
            out.append("\n" * source.count("\n", i, j))
            i = j + 2
        elif c == "#":
            j = source.find("\n", i)
            i = n if j == -1 else j
        elif source.startswith('@"', i) or source.startswith("@'", i):
            m = re.compile("^" + re.escape(source[i + 1]) + "@", re.M).search(source, i + 2)
            assert m, "unterminated here-string"
            out.append(" ")
            i = m.end()
        elif c == "'":
            j = i + 1
            while True:
                k = source.find("'", j)
                assert k != -1, f"unterminated single-quoted string near: {source[i:i + 60]!r}"
                if source.startswith("''", k):
                    j = k + 2
                    continue
                break
            out.append(" ")
            i = k + 1
        elif c == '"':
            j = i + 1
            while True:
                assert j < n, f"unterminated double-quoted string near: {source[i:i + 60]!r}"
                if source[j] == "`":
                    j += 2
                    continue
                if source[j] == '"':
                    if source.startswith('""', j):
                        j += 2
                        continue
                    break
                j += 1
            out.append(" ")
            i = j + 1
        elif c == "`":
            out.append("  ")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


PS_BUILTINS = {
    "Write-Host", "Get-Command", "New-Object", "Join-Path", "Test-Path", "Set-Location", "Start-Process", "Start-Sleep",
    "Get-Date", "Read-Host", "Remove-Item", "New-Item", "Invoke-WebRequest", "Expand-Archive", "Set-Content", "Out-Host",
    "Out-Null", "ForEach-Object", "Split-Path", "Get-NetFirewallRule", "Remove-NetFirewallRule", "New-NetFirewallRule",
}


@pytest.mark.parametrize("script", PS_SCRIPTS, ids=lambda p: p.name)
def test_powershell_scripts_are_ps51_safe(script):
    raw = script.read_text(encoding="utf-8")
    assert raw.isascii(), f"{script.name}: PowerShell 5.1 reads BOM-less files as ANSI - keep them ASCII"
    code = _ps_strip(raw)
    pairs = {")": "(", "}": "{", "]": "["}
    stack: list[tuple[str, int]] = []
    for lineno, line in enumerate(code.splitlines(), 1):
        for ch in line:
            if ch in "({[":
                stack.append((ch, lineno))
            elif ch in pairs:
                assert stack and stack[-1][0] == pairs[ch], f"{script.name}:{lineno}: unbalanced {ch!r}"
                stack.pop()
    assert not stack, f"{script.name}: unclosed {stack[-1][0]!r} from line {stack[-1][1]}"
    for token in ("&&", "||", "??", "?.", "-Parallel", "utf8NoBOM", "-AsByteStream"):
        assert token not in code, f"{script.name}: {token!r} needs PowerShell 7"
    assert not re.search(r"ConvertFrom-SecureString[^\n]*-AsPlainText", code)
    assert not re.search(r"\$(host|profile|args)\s*=(?!=)", code, re.I), "do not assign automatic variables"
    assert not re.search(r"param\s*\([^)]*\$(host|profile|args)\b", code, re.I | re.S)
    if script.name != "common.ps1":
        first = code.strip().split(None, 1)[0]
        assert first.startswith(("[CmdletBinding()]", "param(")), f"{script.name}: param() must come first"
        assert "common.ps1" in raw
        assert '$ErrorActionPreference = "Stop"' in raw


def test_powershell_function_names_resolve():
    defined: set[str] = set()
    for script in PS_SCRIPTS:
        defined |= set(re.findall(r"^\s*function\s+([A-Za-z]+-[A-Za-z]+)", script.read_text(encoding="utf-8"), re.M))
    for script in PS_SCRIPTS:
        code = _ps_strip(script.read_text(encoding="utf-8"))
        used = set(re.findall(r"(?<![\w$-])([A-Z][a-z]+-[A-Z][A-Za-z]+)\b", code))
        unknown = used - defined - PS_BUILTINS
        assert not unknown, f"{script.name}: unknown commands {sorted(unknown)} (typo?)"


def test_setup_ps1_covers_the_windows_install_story():
    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    pgvector = (SCRIPTS / "windows" / "install_pgvector.ps1").read_text(encoding="utf-8")
    common = (SCRIPTS / "windows" / "common.ps1").read_text(encoding="utf-8")
    for needle in ('[ValidateSet("docker", "native", "existing")]', '[string]$DbMode = "docker"', "$WithAiDocker", "$SkipVideos", "$SkipPip",
                   "$SkipSeed", '[string]$PythonVersion = "3.12"', "Python.Python.$PythonVersion", "UB-Mannheim.TesseractOCR",
                   "Docker.DockerDesktop", "PostgreSQL.PostgreSQL.17", "--tesseract", "make_env.py", '"configure", "--db"',
                   '"init-db", "--with-test-db"', "build_assets.py", '"gen-videos"', '"seed"', '"up", "-d", "db", "redis"',
                   '"ollama", "ollama-pull"', "http://localhost:$port", "Memurai", "Test-Admin", "-Verb RunAs", "$PSScriptRoot"):
        assert needle in setup, needle
    for needle in ("Microsoft.VisualStudio.2022.BuildTools", "Microsoft.VisualStudio.Workload.VCTools", "vswhere.exe",
                   "vcvars64.bat", "Makefile.win", "nmake", "PGROOT", "Test-Admin"):
        assert needle in pgvector, needle
    assert "winget" in common and "$LASTEXITCODE" in common
    run = (ROOT / "run.ps1").read_text(encoding="utf-8")
    assert "$Reload" in run and "$Workers" in run and '"-m", "app"' not in run and "-m app" in run and "--workers" in run
    assert "Activate.ps1" in run


# ===================================================================================== Docker
def _compose_services(text: str) -> dict[str, str]:
    services: dict[str, str] = {}
    in_services, current = False, None
    for line in text.splitlines():
        if line.strip() and not line.startswith((" ", "#")):
            in_services, current = line.startswith("services:"), None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = ""
        elif current:
            services[current] += line + "\n"
    return services


def test_compose_file_services():
    text = (ROOT / "docker" / "compose.yml").read_text(encoding="utf-8")
    assert "\t" not in text and ":latest" not in text
    services = _compose_services(text)
    assert set(services) == {"db", "redis", "ollama", "ollama-pull", "kokoro", "app"}
    db = services["db"]
    assert "image: pgvector/pgvector:" in db and "pg17" in db and "5433:5432" in db and "pg_isready" in db and "pgdata:" in db
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD" in db
    redis = services["redis"]
    assert "image: redis:7-alpine" in redis and "6380:6379" in redis and "redis-cli ping" in redis and "redisdata:" in redis
    assert '"--requirepass", "${REDIS_PASSWORD:-change-me}"' in redis and '"--appendonly", "yes"' in redis
    ollama = services["ollama"]
    assert re.search(r"image: ollama/ollama:\d+\.\d+\.\d+\n", ollama) and "11434:11434" in ollama and "ollama:/root/.ollama" in ollama
    pull = services["ollama-pull"]
    assert "ollama pull" in pull and "ollama show" in pull and "gemma3:4b" in pull and "service_healthy" in pull
    assert 'restart: "no"' in pull
    kokoro = services["kokoro"]
    assert "ghcr.io/remsky/kokoro-fastapi-cpu:v" in kokoro and "8880:8880" in kokoro and 'profiles: ["voice"]' in kokoro
    app = services["app"]
    assert 'profiles: ["app"]' in app and "dockerfile: docker/Dockerfile" in app and "env_file: ../.env" in app
    assert "HOST: 0.0.0.0" in app and "db:" in app and "condition: service_healthy" in app and "init-db" in app
    for block in services.values():  # nothing is published beyond this computer
        for port in re.findall(r'- "([^"]+)"', block):
            if ":" in port and port.count(":") >= 2:
                assert port.startswith("127.0.0.1:"), port


def test_gpu_override_and_dockerfile():
    gpu = (ROOT / "docker" / "compose.gpu.yml").read_text(encoding="utf-8")
    assert set(_compose_services(gpu)) == {"ollama", "kokoro"}
    assert "driver: nvidia" in gpu and "capabilities: [gpu]" in gpu and "kokoro-fastapi-gpu" in gpu
    docker = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12-slim$", docker, re.M)
    assert "tesseract-ocr tesseract-ocr-eng" in docker and "pip install -r requirements.txt" in docker
    assert re.search(r"^USER ironpulse$", docker, re.M) and "useradd" in docker
    assert "HEALTHCHECK" in docker and 'CMD ["python", "docker/healthcheck.py"]' in docker
    probe = (ROOT / "docker" / "healthcheck.py").read_text(encoding="utf-8")
    assert "urllib.request" in probe and "/healthz" in probe and "X-Forwarded-Proto" in probe  # tests/test_ai_docker_fallback.py
    assert 'CMD ["python", "-m", "app"]' in docker
    assert not re.search(r"(SECRET_KEY|PASSWORD|API_KEY|ENCRYPTION_KEY|PEPPER)\s*=", docker), "no secrets in the image"
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".venv/", "var/", ".env", "__pycache__/", "!.env.example"):
        assert entry in ignore, entry


DOCKER_CLI = _load("ai_fallback").find_docker()  # PATH, or where Docker Desktop installs it (~/.docker/bin ...)


@pytest.mark.skipif(DOCKER_CLI is None, reason="docker is not installed")
def test_docker_compose_config_validates(tmp_path):
    env = _example_env(tmp_path)
    for name in ("compose.yml", "compose.gpu.yml"):
        args = [DOCKER_CLI, "compose", "--env-file", str(env), "-f", str(ROOT / "docker" / "compose.yml")]
        if name != "compose.yml":
            args += ["-f", str(ROOT / "docker" / name)]
        proc = subprocess.run([*args, "--profile", "voice", "--profile", "app", "config", "-q"], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 and "env file" in proc.stderr and ".env" in proc.stderr:
            pytest.skip("compose needs the project .env for the app service")
        assert proc.returncode == 0, proc.stderr


# ===================================================================================== README + TERMS
DISCLAIMER = ("The owner, the developer and the AI are NOT responsible for anything; this is just a student-grade project; "
              "nothing here is medical, legal or financial advice.")


def test_readme_covers_setup_ai_and_disclaimer():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    flat = " ".join(readme.split())
    assert DISCLAIMER in flat and flat.count("NOT responsible for anything") >= 2
    for needle in ("setup.ps1", "./setup.sh", "--db runtime", "docker compose --env-file .env -f docker/compose.yml",
                   "ARCHITECTURE.md", "TERMS.md", "AI_GATEWAY_BASE_URL", "http://192.168.0.101:8000/v1", "ai-chat",
                   "open_firewall_port.ps1", "New-NetFirewallRule", "var/demo_credentials.txt", "var/mail", "GEOAPIFY_API_KEY",
                   "CASHFREE_", "TEST MODE", "Kokoro", "browser", "dev_watch.py", "auto-migrate", "REDIS_URL", "@username",
                   "member ID", "Forgot username", "privacy", "streamed", "AI can make mistakes", "signed-in users",
                   "Protective-gear", "lip-sync", "mic toggle", "Voice commands", "Safety stop", "Gym shop", "refund",
                   "does not measure heart rate", "TEST_DATABASE_URL", "stress", "Troubleshooting", "Project structure",
                   "30 days", "Apple Vision", "Windows OCR", "Visual Studio 2022 Build Tools", "Memurai"):
        assert needle.lower() in readme.lower(), needle
    for command in COMMANDS:
        assert f"`{command}" in readme, command


def test_docs_contain_no_real_secrets():
    env = ROOT / ".env"
    if not env.is_file():
        pytest.skip("no local .env")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = make_env.parse(env.read_text(encoding="utf-8"))
    secrets = [v for k, v in values.items()
               if any(w in k for w in ("SECRET", "PASSWORD", "KEY", "PEPPER", "URL")) and len(v) >= 12 and v not in example]
    secrets += [m for v in values.values() for m in re.findall(r"://[^:/@]*:([^@]{8,})@", v)]
    demo = get_settings().var_dir / "demo_credentials.txt"
    for candidate in (ROOT / "var" / "demo_credentials.txt", demo):
        if candidate.is_file():
            secrets += re.findall(r"^password: (\S+)", candidate.read_text(encoding="utf-8"), re.M)
    owned = [ROOT / "README.md", ROOT / "TERMS.md", ROOT / "docker" / "compose.yml", ROOT / "docker" / "compose.gpu.yml",
             ROOT / "docker" / "Dockerfile", *SHELL_SCRIPTS, *PS_SCRIPTS, SCRIPTS / "manage.py", SCRIPTS / "check_env.py", SCRIPTS / "init_db.py"]
    for path in owned:
        text = path.read_text(encoding="utf-8")
        for secret in secrets:
            if "@" in secret and "://" in secret:  # whole URLs: only their passwords matter
                continue
            assert secret not in text, f"a value from .env leaked into {path.name}"


def _terms_sections_from_template() -> list[tuple[int, str]]:
    template = (ROOT / "app" / "templates" / "public" / "terms.html").read_text(encoding="utf-8")
    s = get_settings()
    values = {"offline_km": str(int(s.offline_max_distance_km)), "retention_days": str(s.account_retention_days)}
    sections = []
    for num, title in re.findall(r"<h2>(\d+)\.\s*(.*?)</h2>", template):
        title = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), title)
        sections.append((int(num), html.unescape(title).strip()))
    return sections


def test_terms_md_mirrors_the_terms_page():
    terms = (ROOT / "TERMS.md").read_text(encoding="utf-8")
    expected = _terms_sections_from_template()
    assert len(expected) == 19
    actual = [(int(n), t.strip()) for n, t in re.findall(r"^## (\d+)\. (.+)$", terms, re.M)]
    assert actual == expected
    template = (ROOT / "app" / "templates" / "public" / "terms.html").read_text(encoding="utf-8")
    items = re.findall(r'"([^"]+)"', re.search(r"\{% for item in \[(.*?)\] %\}", template, re.S).group(1))
    assert len(items) == 12
    for item in items:
        assert f"- {item}" in terms, item
    flat = " ".join(terms.split())
    for needle in ("student-grade project", "NOT responsible for anything", "AI can make mistakes", "112", "911",
                   "does not measure your heart rate", "Kokoro", "Redis and PostgreSQL", "kept for 30 days", "within **30 km**",
                   "within 1.5 km", "at least 3 photos", "TEST MODE", "within 7 days"):
        assert needle in flat, needle
