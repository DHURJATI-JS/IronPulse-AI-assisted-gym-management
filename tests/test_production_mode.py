"""ENVIRONMENT=production is secure by default (app/config.py ``Settings._production_mode``, README "Deploying to
production").

* COOKIE_SECURE / FORCE_HTTPS / TRUST_PROXY_HEADERS default to true in production unless set explicitly; an explicit
  false is honoured and logged as a WARNING at every start; DEBUG and RELOAD are always off;
* unsafe settings stop the start: placeholder / weak secrets, a localhost-only ALLOWED_HOSTS on a non-loopback HOST,
  ALLOWED_HOSTS no request can match, '*' hosts, '*' or 'null' CORS origins, (near-)catch-all TRUSTED_PROXIES, a weak
  SUPERADMIN_PASSWORD, the simulated AI without AI_SIMULATED_ALLOWED - each as one line naming the variable, never a
  value, never a traceback (exit status 3, so a multi-worker uvicorn stops instead of restarting its workers); a
  malformed host wildcard stops every environment (Starlette would 500 every request);
* settings that only half work are warned about with level and source even before logging is configured: Host entries
  that never match, a PUBLIC_BASE_URL on localhost or outside ALLOWED_HOSTS or not https;
* ``manage.py check`` reports it ('Production settings'), ``manage.py run`` prints the warnings, .env.example no longer
  pins the development values, and the load-test launcher's environment (loadtest/ltenv.py) still starts cleanly.

Every rule is tested in both directions. Settings are built with ``_env_file=None`` and the relevant variables removed
from the environment, so the developer's .env never decides a result (except where a test says so).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from pydantic import ValidationError

import app.config as config_module
from app.config import (
    ConfigError,
    Settings,
    build_settings,
    get_settings,
    reset_settings_cache,
    startup_warnings,
)

ROOT = Path(__file__).resolve().parent.parent
SECRET = "pT9-vK2_mQ7xZ4rW8nB5cH3jL6dF1gY0sA7eU4iO2"
PEPPER = "Lq4-Wm8_Zt2Kc6Rn9Hy3"
FERNET = Fernet.generate_key().decode()
STRONG = {"secret_key": SECRET, "data_encryption_key": FERNET, "id_hash_pepper": PEPPER}
SITE = {"allowed_hosts": "gym.example.com", "public_base_url": "https://gym.example.com"}  # a real site's names
INTRANET = {"allowed_hosts": "intranet.local", "public_base_url": "http://intranet.local"}  # a plain-HTTP trial
ENV_KEYS = ("ENVIRONMENT", "DEBUG", "RELOAD", "HOST", "PUBLIC_BASE_URL", "ALLOWED_HOSTS", "CORS_ORIGINS", "COOKIE_SECURE",
            "FORCE_HTTPS", "TRUST_PROXY_HEADERS", "TRUSTED_PROXIES", "SECRET_KEY", "DATA_ENCRYPTION_KEY",
            "ID_HASH_PEPPER", "SUPERADMIN_PASSWORD", "AI_PROVIDER_ORDER", "AI_SIMULATED_ALLOWED")


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


check_env = _load("check_env", ROOT / "scripts" / "check_env.py")
manage = _load("manage", ROOT / "scripts" / "manage.py")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def prod(**values: Any) -> Settings:
    """Production settings from these values only (no .env), strong secrets unless overridden."""
    return build_settings(_env_file=None, environment="production", **{**STRONG, **values})


def dev(**values: Any) -> Settings:
    return build_settings(_env_file=None, environment="development", **{**STRONG, **values})


def refused(**values: Any) -> list[str]:
    with pytest.raises(ConfigError) as err:
        prod(**values)
    return err.value.problems


# ===================================================================================== secure defaults
def test_production_turns_on_the_secure_defaults_and_development_keeps_its_own():
    s = prod(debug=True, reload=True)
    assert (s.cookie_secure, s.force_https, s.trust_proxy_headers) == (True, True, True)
    assert (s.debug, s.reload) == (False, False)
    d = dev(debug=True, reload=True)
    assert (d.cookie_secure, d.force_https, d.trust_proxy_headers) == (False, False, False)
    assert (d.debug, d.reload) == (True, True)


def test_the_environment_variable_alone_switches_production_on(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = build_settings(_env_file=None, **STRONG)
    assert s.is_production and s.cookie_secure and s.force_https and s.trust_proxy_headers


def test_an_explicit_false_is_honoured_and_warned_about(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("FORCE_HTTPS", "false")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    # an https site never runs on a stale .env's insecure cookies / plain HTTP (review #7): refused, one line each
    assert [p.split("=", 1)[0] for p in refused(**SITE)] == ["COOKIE_SECURE", "FORCE_HTTPS"]
    s = prod(**INTRANET)  # a plain-HTTP intranet trial: the explicit opt-outs are honoured and warned about
    assert (s.cookie_secure, s.force_https, s.trust_proxy_headers) == (False, False, False)
    warnings = s.production_warnings()
    assert [w.split("=", 1)[0] for w in warnings] == ["COOKIE_SECURE", "FORCE_HTTPS", "TRUST_PROXY_HEADERS"]
    assert "HSTS" in warnings[1] and "loop" not in warnings[2]  # no HTTPS redirect, so no redirect loop either
    # explicitly true (or simply left out): nothing to warn about
    assert prod(**SITE, cookie_secure=True, force_https=True,
                trust_proxy_headers=True).production_warnings() == []
    for key in ("COOKIE_SECURE", "FORCE_HTTPS", "TRUST_PROXY_HEADERS"):
        monkeypatch.delenv(key)
    assert prod(**SITE).production_warnings() == []
    # outside production nothing is ever warned about
    assert dev(cookie_secure=False).production_warnings() == []


def test_trust_proxy_headers_false_with_https_redirect_warns_about_the_loop():
    s = prod(**SITE, trust_proxy_headers=False)
    assert s.force_https and any("redirects in a loop" in w for w in s.production_warnings())


def test_get_settings_logs_the_warnings_at_every_start(monkeypatch, caplog):
    for key, value in {"ENVIRONMENT": "production", "SECRET_KEY": SECRET, "DATA_ENCRYPTION_KEY": FERNET,
                       "ID_HASH_PEPPER": PEPPER, "SUPERADMIN_PASSWORD": "", "HOST": "127.0.0.1",
                       "ALLOWED_HOSTS": "gym.example.com", "PUBLIC_BASE_URL": "http://gym.example.com",
                       "COOKIE_SECURE": "false"}.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    try:
        with caplog.at_level(logging.WARNING, logger="ironpulse.config"):
            first = get_settings()
            reset_settings_cache()  # a second process start
            get_settings()
    finally:
        monkeypatch.undo()
        reset_settings_cache()  # the next caller rebuilds the test settings
    lines = [r for r in caplog.records if r.name == "ironpulse.config" and "COOKIE_SECURE=false" in r.getMessage()]
    assert len(lines) == 2 and all(r.levelno == logging.WARNING for r in lines)
    assert first.is_production and not first.cookie_secure
    assert get_settings().is_test  # back to the suite's own settings


# ===================================================================================== allowed hosts
def test_localhost_only_hosts_stop_a_server_that_listens_on_the_network():
    for host in ("0.0.0.0", "::", "192.168.1.20", "gym-server"):
        problems = refused(host=host)
        assert len(problems) == 1 and problems[0].startswith("ALLOWED_HOSTS ") and f"HOST={host}" in problems[0]
        assert "ALLOWED_HOSTS=gym.example.com" in problems[0]
    assert refused(host="0.0.0.0", allowed_hosts="localhost,127.0.0.1")[0].startswith("ALLOWED_HOSTS ")
    # a real host name, or a server only this machine reaches (behind a local proxy)
    assert prod(host="0.0.0.0", allowed_hosts="gym.example.com").allowed_hosts == ["gym.example.com"]
    for loopback in ("127.0.0.1", "::1", "[::1]", "localhost"):
        assert prod(host=loopback).host == loopback
    # development keeps answering on the LAN as before
    assert dev(host="0.0.0.0").allowed_hosts == ["localhost", "127.0.0.1", "::1"]


def test_unset_hosts_on_a_loopback_server_start_with_a_warning():
    assert any(w.startswith("ALLOWED_HOSTS is not set") for w in prod(host="127.0.0.1").production_warnings())
    explicit = prod(host="127.0.0.1", allowed_hosts="127.0.0.1,localhost")  # the load-test launcher's choice
    assert not any(w.startswith("ALLOWED_HOSTS") for w in explicit.production_warnings())


def test_wildcard_or_empty_hosts_are_refused():
    assert refused(allowed_hosts="*")[0].startswith("ALLOWED_HOSTS=* accepts any Host header")
    assert refused(allowed_hosts="")[0].startswith("ALLOWED_HOSTS is empty")
    assert dev(allowed_hosts="*").allowed_hosts == ["*"]  # development: unchanged


# ===================================================================================== secrets
@pytest.mark.parametrize(("field", "value", "why"), [
    ("secret_key", "change-me-run-setup-to-generate-a-64-char-secret", "is a placeholder"),
    ("secret_key", "a" * 48, "is not random"),
    ("secret_key", "my-example-secret-key-for-the-gym-0123456789", "is a placeholder"),
    ("id_hash_pepper", "y" * 24, "is not random"),
    ("id_hash_pepper", "x" * 24, "is a placeholder"),
    ("id_hash_pepper", "changeme-pepper-123", "is a placeholder"),
    ("data_encryption_key", "z" * 44, "is not a valid Fernet key"),
    ("data_encryption_key", "change-me-run-setup-to-generate-a-fernet-key", "is not a valid Fernet key"),
])
def test_placeholder_or_weak_secrets_stop_production(field, value, why):
    problems = refused(**{field: value})
    assert len(problems) == 1 and problems[0].startswith(f"{field.upper()} {why}")
    assert "python scripts/make_env.py" in problems[0] and value not in problems[0]
    assert getattr(dev(**{field: value}), field) == value  # development still starts with them


def test_short_or_missing_secrets_name_the_variable_in_every_environment():
    for build in (prod, dev):
        with pytest.raises(ConfigError) as err:
            build(secret_key="too-short")
        assert err.value.problems == ["SECRET_KEY is too short (at least 32 characters) - set SECRET_KEY in .env or the "
                                      "environment (python scripts/make_env.py generates it)"]
    with pytest.raises(ConfigError) as err:
        build_settings(_env_file=None, environment="production", data_encryption_key=FERNET, id_hash_pepper=PEPPER)
    assert err.value.problems[0].startswith("SECRET_KEY is not set - set SECRET_KEY")


def test_strong_random_secrets_pass():
    for _ in range(20):  # random keys (hex too) never trip the 'not random' rule
        key = Fernet.generate_key().decode()
        s = prod(secret_key=os.urandom(32).hex(), id_hash_pepper=os.urandom(16).hex(), data_encryption_key=key)
        assert s.is_production and s.data_encryption_key == key


# ===================================================================================== CORS, proxies, admin password
def test_cors_wildcard_is_refused_in_production_only():
    assert refused(cors_origins="*")[0].startswith("CORS_ORIGINS=* lets any website")
    assert prod(cors_origins="https://shop.example.com").cors_origins == ["https://shop.example.com"]
    assert dev(cors_origins="*").cors_origins == ["*"]


def test_catch_all_trusted_proxies_are_refused_while_proxy_headers_are_trusted():
    for catch_all in ("0.0.0.0/0", "::/0", "127.0.0.1,0.0.0.0/0"):
        problems = refused(trusted_proxies=catch_all)
        assert problems[0].startswith("TRUSTED_PROXIES=") and "dodge bans" in problems[0]
    assert prod(trusted_proxies="10.0.0.0/8,127.0.0.1").trust_proxy_headers
    # headers not trusted at all: the list is never used (that opt-out is a warning of its own)
    untrusted = prod(trusted_proxies="0.0.0.0/0", trust_proxy_headers=False, allowed_hosts="gym.example.com")
    assert untrusted.trust_proxy_headers is False


@pytest.mark.parametrize("near_catch_all", [
    "0.0.0.0/1,128.0.0.0/1",  # the whole IPv4 internet in two halves
    "64.0.0.0/2", "1.0.0.0/7",
    "::ffff:0:0/96",  # every IPv4 client of a dual-stack listener (reported as ::ffff:a.b.c.d)
    "::ffff:0:0/97", "::/8", "2000::/3",
])
def test_near_catch_all_trusted_proxies_are_refused(near_catch_all):
    problems = refused(trusted_proxies=f"127.0.0.1,{near_catch_all}")
    assert len(problems) == 1 and problems[0].startswith(f"TRUSTED_PROXIES={near_catch_all.split(',')[0]} ")
    assert "dodge bans" in problems[0]
    assert dev(trusted_proxies=near_catch_all).trusted_proxies  # development: unchanged


def test_uvicorns_trust_everyone_star_is_refused_like_a_catch_all():
    """Review #0: '*' is uvicorn's "trust every client"; python -m app hands TRUSTED_PROXIES to forwarded_allow_ips."""
    for star in ("*", "127.0.0.1,*", " * "):
        problems = refused(trusted_proxies=star)
        assert problems[0].startswith("TRUSTED_PROXIES=* ") and "dodge bans" in problems[0], problems
    assert dev(trusted_proxies="*").trusted_proxies == ["*"]  # development: unchanged
    # a name is not an address: the app ignores it while uvicorn would take it literally
    assert refused(trusted_proxies="proxy.internal")[0].startswith("TRUSTED_PROXIES=proxy.internal is not an IP")


def test_python_m_app_hands_uvicorn_only_addresses_and_networks(monkeypatch):
    from app.config import uvicorn_forwarded_allow_ips

    assert uvicorn_forwarded_allow_ips(dev(trusted_proxies="10.0.0.0/8,*,proxy.local,::1")) == "10.0.0.0/8,::1"
    assert uvicorn_forwarded_allow_ips(dev(trusted_proxies="*")) == "127.0.0.1"
    import uvicorn

    import app.__main__ as entry

    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "_ensure_environment", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(config_module, "get_settings", lambda: dev(trusted_proxies="127.0.0.1,*"))
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv(entry.WORKERS_ENV, "1")  # main() sets it for its workers: restored after the test
    entry.main()
    assert seen["forwarded_allow_ips"] == "127.0.0.1"


def test_forwarded_allow_ips_star_is_refused_in_production(monkeypatch):
    """Review #6: uvicorn's CLI (manage.py run --workers, a container image) reads $FORWARDED_ALLOW_IPS itself."""
    for value in ("*", "127.0.0.1,*", "0.0.0.0/0"):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        assert refused()[0].startswith("FORWARDED_ALLOW_IPS=* makes uvicorn believe"), value
        assert dev().is_production is False  # development: never refused
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,10.0.0.0/8")
    assert prod().is_production


def test_private_ipv6_proxy_ranges_are_not_catch_alls():
    """Review #14: ULA and link-local IPv6 (Docker IPv6 networks) can't be reached from the internet."""
    assert prod(trusted_proxies="fd00::/8,fe80::/10,fc00::/7").trusted_proxies == ["fd00::/8", "fe80::/10", "fc00::/7"]
    for wide in ("2000::/3", "::ffff:0:0/96", "::/0"):
        assert refused(trusted_proxies=wide)[0].startswith(f"TRUSTED_PROXIES={wide} "), wide


def test_production_allows_the_simulated_ai_only_alone():
    """Review #13: 'gateway,simulated' would answer members with generated text whenever the model fails."""
    problems = refused(ai_provider_order="gateway,simulated", ai_simulated_allowed=True)
    assert len(problems) == 1 and problems[0].startswith("AI_PROVIDER_ORDER=gateway,simulated would answer members")
    assert prod(ai_provider_order="simulated", ai_simulated_allowed=True).ai_provider_order == ["simulated"]
    assert dev(ai_provider_order="gateway,simulated").ai_provider_order == ["gateway", "simulated"]


def test_real_proxy_ranges_stay_allowed():
    """Private networks, a Docker bridge, CDN blocks (e.g. Cloudflare's widest, 104.16.0.0/13) and a mapped /8."""
    ranges = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,104.16.0.0/13,2400:cb00::/32,2a06:98c0::/29,::ffff:10.0.0.0/104"
    assert prod(trusted_proxies=ranges).trusted_proxies == ranges.split(",")


def test_cors_null_origin_is_refused_in_production_only():
    for origins in ("null", "https://shop.example.com,null", "NULL"):
        problems = refused(cors_origins=origins)
        assert problems == ["CORS_ORIGINS=null lets a sandboxed frame on any website send signed-in requests - remove "
                            "'null' from CORS_ORIGINS"]
    assert prod(cors_origins="https://shop.example.com").cors_origins == ["https://shop.example.com"]
    assert dev(cors_origins="null").cors_origins == ["null"]


@pytest.mark.parametrize("pattern", ["*example.com", "gym.*.com", "*gym.example.com", "gym.example.*", "a*"])
def test_malformed_host_wildcards_stop_every_environment_with_one_line(pattern):
    """Starlette's TrustedHostMiddleware asserts the wildcard shape only when the app builds its middleware - at the
    first request - so the server would start and then answer every request with a 500 and a traceback."""
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    with pytest.raises(AssertionError):
        TrustedHostMiddleware(lambda *a: None, allowed_hosts=["localhost", pattern])
    for build in (prod, dev):
        with pytest.raises(ConfigError) as err:
            build(host="127.0.0.1", allowed_hosts=f"localhost,{pattern}")
        assert err.value.problems == [f"ALLOWED_HOSTS is invalid: '{pattern}' - a wildcard must look like "
                                      "*.example.com - set ALLOWED_HOSTS in .env or the environment"]


def test_wildcard_subdomains_and_exact_names_pass():
    assert prod(allowed_hosts="*.example.com,gym.example.com").allowed_hosts == ["*.example.com", "gym.example.com"]
    assert dev(allowed_hosts="*").allowed_hosts == ["*"]


@pytest.mark.parametrize(("pattern", "suffix"), [
    ("*.com", "com"), ("*.co.in", "co.in"), ("*.com.au", "com.au"), ("*.org.uk", "org.uk"), ("*.io", "io"),
    ("*.duckdns.org", "duckdns.org"), ("*.nip.io", "nip.io"), ("*.COM.", "com"),
])
def test_a_public_suffix_host_wildcard_stops_production(pattern, suffix):
    """'*.com' passed the Host check for every .com name: an attacker's own domain pointed at the server (DNS
    rebinding) was answered. Starlette's TrustedHostMiddleware really accepts it - so production refuses it."""
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    assert TrustedHostMiddleware(lambda *a: None, allowed_hosts=["*.com"]).allowed_hosts == ["*.com"]
    problems = refused(allowed_hosts=f"gym.example.com,{pattern}", public_base_url="https://gym.example.com")
    assert problems == [f"ALLOWED_HOSTS={pattern} accepts every name under the public suffix '.{suffix}' - anyone can "
                        "register one and point it at this server (DNS rebinding); name your own domain, e.g. "
                        "ALLOWED_HOSTS=*.gym.example.com or gym.example.com"]
    assert dev(allowed_hosts=pattern).allowed_hosts == [pattern]  # development: unchanged


def test_wildcards_of_ones_own_domain_or_a_private_name_still_pass():
    for hosts in ("*.gym.example.com", "*.example.com", "*.gym.co.in", "*.mygym.duckdns.org", "*.local",
                  "*.internal", "gym.com"):
        s = prod(allowed_hosts=f"gym.example.com,{hosts}", public_base_url="https://gym.example.com")
        assert s.allowed_hosts == ["gym.example.com", hosts]


def test_a_plain_http_cors_origin_stops_production_and_is_warned_about_on_an_http_trial():
    problems = refused(cors_origins="https://shop.example.com,http://evil.example", **SITE)
    assert problems == ["CORS_ORIGINS=http://evil.example is a plain-http:// origin - anyone on that page's network "
                        "path can rewrite it and send signed-in requests from it; list https:// origins only (or leave "
                        "CORS_ORIGINS empty)"]
    ok = prod(cors_origins="https://shop.example.com,http://localhost:5173,http://127.0.0.1:3000", **SITE)
    assert not any(w.startswith("CORS_ORIGINS") for w in ok.production_warnings())
    # a plain-HTTP intranet trial (COOKIE_SECURE=false explicitly) starts - and hears why it should not stay that way
    trial = prod(cors_origins="http://intranet.local:3000", cookie_secure=False, force_https=False, **INTRANET)
    assert any(w.startswith("CORS_ORIGINS=http://intranet.local:3000 is a plain-http:// origin")
               for w in trial.production_warnings())
    assert dev(cors_origins="http://evil.example").cors_origins == ["http://evil.example"]


def test_host_entries_that_never_match_are_warned_about():
    for entry in ("https://gym.example.com", "gym.example.com:443", "gym.example.com/app"):
        s = prod(allowed_hosts=f"gym.example.com,{entry}", public_base_url="https://gym.example.com")
        assert s.production_warnings() == [f"ALLOWED_HOSTS contains '{entry}', which never matches a request (the "
                                           "Host header is compared without scheme, port or path) - list bare host "
                                           "names, e.g. ALLOWED_HOSTS=gym.example.com"]
    # bare names, wildcards and IPv6 literals: nothing to say
    clean = prod(allowed_hosts="gym.example.com,*.example.com,[::1],::1", public_base_url="https://gym.example.com")
    assert clean.production_warnings() == []


def test_hosts_of_which_none_can_match_stop_production():
    """Every request would get 400 'Invalid host header': refused at the start instead of serving nothing."""
    for hosts in ("https://gym.example.com", "gym.example.com:443,localhost:10000"):
        problems = refused(host="0.0.0.0", allowed_hosts=hosts)
        first = hosts.split(",")[0]
        assert problems == [f"ALLOWED_HOSTS has no entry a request can match ('{first}': the Host header is compared "
                            "without scheme, port or path) - list bare host names, e.g. ALLOWED_HOSTS=gym.example.com"]
    assert prod(host="0.0.0.0", allowed_hosts="gym.example.com,gym.example.com:443").allowed_hosts  # one usable: warned
    assert dev(allowed_hosts="https://gym.example.com").allowed_hosts == ["https://gym.example.com"]


@pytest.mark.parametrize(("hosts", "base", "accepted"), [
    ("localhost,127.0.0.1,::1", "https://gym.example.com", False),  # an older .env.example's pinned line
    ("www.example.org", "https://example.org", True),  # the Host check redirects to the www. name
    ("*.example.com", "https://gym.example.com", True),
    ("*.example.com", "https://example.com", False),  # a '*.' wildcard never covers the bare domain
    ("GYM.example.com", "https://gym.EXAMPLE.com", True),  # host names are compared lower-case
    ("gym.example.com", "https://gym.example.com:8443/app", True),  # port and path are not part of the name
])
def test_a_public_base_url_the_host_check_refuses_is_warned_about(hosts, base, accepted):
    warnings = prod(allowed_hosts=hosts, public_base_url=base).production_warnings()
    named = [w for w in warnings if "names a host ALLOWED_HOSTS does not accept" in w]
    if accepted:
        assert named == []
    else:
        host = base.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
        assert named == [f"PUBLIC_BASE_URL={base} names a host ALLOWED_HOSTS does not accept: links in e-mails would "
                         f"be answered 400 'Invalid host header' - add {host} to ALLOWED_HOSTS"]
    # development never warns
    assert dev(allowed_hosts=hosts, public_base_url=base).production_warnings() == []


@pytest.mark.parametrize(("password", "why"), [
    ("admin123", "is too short"), ("Password123", "is a well-known password"), ("superadmin", "is a well-known password"),
    ("aaaaaaaaaaaaaa", "is not random enough"),
])
def test_weak_superadmin_password_stops_production(password, why):
    problems = refused(superadmin_password=password)
    assert problems[0].startswith(f"SUPERADMIN_PASSWORD {why}") and password not in problems[0]
    assert dev(superadmin_password=password).superadmin_password == password


def test_strong_or_absent_superadmin_password_passes():
    assert prod(superadmin_password="Lt-IronPulse#2026-load").superadmin_password  # the load test's password
    assert prod(superadmin_password=None).superadmin_password is None


# ===================================================================================== simulated AI
def test_simulated_ai_needs_the_explicit_flag_in_production():
    problems = refused(ai_provider_order="simulated")
    assert problems == ["AI_PROVIDER_ORDER names the simulated AI provider (generated answers, no model) - remove "
                        "'simulated' from AI_PROVIDER_ORDER, or set AI_SIMULATED_ALLOWED=true for a load test"]
    allowed = prod(ai_provider_order="simulated", ai_simulated_allowed=True)
    assert allowed.ai_provider_order == ["simulated"]
    loud = [w for w in startup_warnings(allowed) if "SIMULATED AI PROVIDER ACTIVE" in w]
    assert len(loud) == 1 and "members get simulated answers" in loud[0]
    # development: no flag needed, still announced
    assert any("SIMULATED AI PROVIDER ACTIVE" in w for w in startup_warnings(dev(ai_provider_order="simulated")))
    assert startup_warnings(dev()) == []


# ===================================================================================== public base URL
def test_public_base_url_warnings():
    # sign-in and reset links over plain HTTP while the cookies need HTTPS: refused (review #7, was a warning)
    problems = refused(allowed_hosts="gym.example.com", public_base_url="http://gym.example.com")
    assert len(problems) == 1 and problems[0].startswith("PUBLIC_BASE_URL=http://gym.example.com is not https://")
    intranet = prod(**INTRANET, cookie_secure=False, force_https=False)  # the plain-HTTP trial says so explicitly
    assert [w.split("=", 1)[0] for w in intranet.production_warnings()] == ["COOKIE_SECURE", "FORCE_HTTPS"]
    https = prod(allowed_hosts="gym.example.com", public_base_url="https://gym.example.com")
    assert https.production_warnings() == []
    local = prod(host="0.0.0.0", allowed_hosts="gym.example.com", public_base_url="http://localhost:10000")
    assert any("points at this machine" in w for w in local.production_warnings())
    # a production-mode trial on this machine (the load test): nothing to say
    assert prod(host="127.0.0.1", allowed_hosts="127.0.0.1", public_base_url="https://127.0.0.1:12000") \
        .production_warnings() == []


def test_a_forgotten_public_base_url_behind_a_local_proxy_is_warned_about():
    """The README's setup: uvicorn on 127.0.0.1 behind a proxy, ALLOWED_HOSTS=gym.example.com. PUBLIC_BASE_URL left
    at its localhost default would put http://localhost links into sign-in and password-reset e-mails."""
    forgotten = prod(host="127.0.0.1", allowed_hosts="gym.example.com")
    assert forgotten.production_warnings() == [
        f"PUBLIC_BASE_URL={forgotten.public_base_url} points at this machine: links in e-mails do not work for anyone "
        "else - set PUBLIC_BASE_URL to the site's address, e.g. https://gym.example.com"]
    assert prod(host="127.0.0.1", **SITE).production_warnings() == []


# ===================================================================================== error format
def test_every_problem_is_one_line_naming_the_variable_and_never_a_value():
    placeholder = "change-me-run-setup-to-generate-a-64-char-secret"
    with pytest.raises(ConfigError) as err:
        prod(secret_key=placeholder, host="0.0.0.0", cors_origins="*")
    exc = err.value
    assert isinstance(exc, SystemExit) and exc.code == ConfigError.EXIT_STATUS == 3
    assert [p.split(" ", 1)[0].split("=", 1)[0] for p in exc.problems] == ["SECRET_KEY", "ALLOWED_HOSTS", "CORS_ORIGINS"]
    assert all("\n" not in p for p in exc.problems) and "\n" not in str(exc) and placeholder not in str(exc)
    # pydantic's own error text never echoes the input either (secrets, database passwords)
    with pytest.raises(ValidationError) as raw:
        Settings(_env_file=None, environment="production", **{**STRONG, "secret_key": placeholder,
                                                               "database_url": "postgresql://u:pw-SECRET-123@db/x"})
    assert placeholder not in str(raw.value) and "pw-SECRET-123" not in str(raw.value)


def test_config_error_report_and_pickling():
    exc = ConfigError(["A is wrong - set A", "B is wrong - set B"])
    lines: list[str] = []
    exc.report(SimpleNamespace(write=lines.append, flush=lambda: None))
    text = "".join(lines)
    assert text == ("[ironpulse] configuration error: A is wrong - set A\n"
                    "[ironpulse] configuration error: B is wrong - set B\n")
    import pickle

    again = pickle.loads(pickle.dumps(exc))
    assert again.problems == exc.problems and again.code == 3


# ===================================================================================== the real start
def _start(args: list[str], env: dict[str, str], timeout: float = 90) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    full.update({"ENVIRONMENT": "production", "SECRET_KEY": SECRET, "DATA_ENCRYPTION_KEY": FERNET,
                 "ID_HASH_PEPPER": PEPPER, "SUPERADMIN_PASSWORD": "", "AI_PROVIDER_ORDER": "gateway,docker,anthropic",
                 "PYTHONUNBUFFERED": "1", **env})
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), env=full, capture_output=True, text=True,
                          timeout=timeout)


IMPORT_APP = ["-c", "import app.main"]


def test_a_refused_start_is_one_line_per_problem_without_a_traceback():
    proc = _start(IMPORT_APP, {"HOST": "0.0.0.0", "ALLOWED_HOSTS": "localhost", "CORS_ORIGINS": "*"})
    lines = [line for line in proc.stderr.splitlines() if line.startswith("[ironpulse] configuration error: ")]
    assert proc.returncode == 3, proc.stderr
    assert "Traceback" not in proc.stderr and "ValidationError" not in proc.stderr and len(lines) == 2
    assert "ALLOWED_HOSTS" in lines[0] and "CORS_ORIGINS" in lines[1]
    assert SECRET not in proc.stderr and FERNET not in proc.stderr


def test_a_good_production_start_logs_the_explicit_opt_outs():
    # a local .env may pin COOKIE_SECURE / TRUST_PROXY_HEADERS (older .env.example): this start sets them itself
    proc = _start(IMPORT_APP, {"HOST": "0.0.0.0", "ALLOWED_HOSTS": "gym.example.com",
                               "PUBLIC_BASE_URL": "https://gym.example.com", "FORCE_HTTPS": "true",
                               "COOKIE_SECURE": "true", "TRUST_PROXY_HEADERS": "false"})
    assert proc.returncode == 0, proc.stderr
    assert "TRUST_PROXY_HEADERS=false in production" in proc.stderr and "Traceback" not in proc.stderr
    assert "COOKIE_SECURE=false" not in proc.stderr and "FORCE_HTTPS=false" not in proc.stderr
    # logged before app.main configures logging, yet with its level and source (a log scan for WARNING finds it)
    lines = [line for line in proc.stderr.splitlines() if "TRUST_PROXY_HEADERS=false in production" in line]
    assert len(lines) == 1 and " WARNING ironpulse.config: TRUST_PROXY_HEADERS=false in production" in lines[0], lines


def test_a_production_start_with_the_simulated_ai_warns_loudly_with_level_and_source():
    proc = _start(IMPORT_APP, {"HOST": "127.0.0.1", "ALLOWED_HOSTS": "127.0.0.1,localhost",
                               "PUBLIC_BASE_URL": "https://127.0.0.1:12000", "COOKIE_SECURE": "true",
                               "FORCE_HTTPS": "true", "TRUST_PROXY_HEADERS": "true", "AI_PROVIDER_ORDER": "simulated",
                               "AI_SIMULATED_ALLOWED": "true", "AI_SIM_TTS": "true"})
    assert proc.returncode == 0 and "Traceback" not in proc.stderr, proc.stderr
    loud = [line for line in proc.stderr.splitlines() if "SIMULATED AI PROVIDER ACTIVE" in line]
    assert len(loud) == 1 and " WARNING ironpulse.ai: !!! SIMULATED AI PROVIDER ACTIVE" in loud[0], loud
    assert "members get simulated answers" in loud[0]
    voice = [line for line in proc.stderr.splitlines() if "SIMULATED VOICE ACTIVE" in line]
    assert len(voice) == 1 and " WARNING ironpulse.ai: " in voice[0], voice


def test_python_m_app_refuses_with_the_same_line():
    if not (ROOT / ".env").is_file():
        pytest.skip("python -m app would create .env first")
    proc = _start(["-m", "app"], {"HOST": "127.0.0.1", "AI_PROVIDER_ORDER": "simulated"})
    assert proc.returncode == 3 and "Traceback" not in proc.stderr
    assert proc.stderr.strip().splitlines()[-1].startswith("[ironpulse] configuration error: AI_PROVIDER_ORDER names "
                                                           "the simulated AI provider")


def test_uvicorn_workers_stop_instead_of_restarting_forever():
    started = time.monotonic()
    proc = _start(["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0", "--workers", "2"],
                  {"HOST": "0.0.0.0", "ALLOWED_HOSTS": "localhost"}, timeout=120)
    assert time.monotonic() - started < 110
    assert "failed to start, stopping the parent process" in proc.stderr, proc.stderr[-2000:]
    assert "Traceback" not in proc.stderr and "[ironpulse] configuration error: ALLOWED_HOSTS" in proc.stderr
    assert "died" not in proc.stderr  # never restarted


# ===================================================================================== check_env / manage.py / files
def test_check_env_reports_production_settings():
    warned = check_env.check_production(check_env.Ctx(settings=prod(**INTRANET, cookie_secure=False,
                                                                    force_https=False),
                                                      load_app_settings=False))
    assert warned.status == check_env.WARN and warned.detail.startswith("COOKIE_SECURE=false in production")
    clean = check_env.check_production(check_env.Ctx(settings=prod(**SITE),
                                                     load_app_settings=False))
    assert clean.status == check_env.OK and "secure defaults in force" in clean.detail
    info = check_env.check_production(check_env.Ctx(settings=dev(), load_app_settings=False))
    assert info.status == check_env.INFO


def test_check_env_shows_a_refused_configuration_instead_of_crashing(monkeypatch):
    def refuse() -> Settings:
        raise ConfigError(["ALLOWED_HOSTS is empty - set ALLOWED_HOSTS", "CORS_ORIGINS=* lets any website - set it"])

    monkeypatch.setattr(config_module, "get_settings", refuse)
    ctx = check_env.Ctx(network=False, db=False)
    assert ctx.settings is None and ctx.config_problems[0].startswith("ALLOWED_HOSTS is empty")
    loaded = check_env.check_settings(ctx)
    assert loaded.status == check_env.FAIL and loaded.critical and "CORS_ORIGINS=*" in loaded.detail
    assert "Deploying to production" in loaded.fix
    assert check_env.check_production(ctx).status == check_env.SKIP
    # a probe that loads the settings itself is reported, never ends the whole report
    monkeypatch.setattr(check_env, "check_redis", lambda ctx: refuse())
    report = check_env.Report(check_env.collect(ctx))
    assert report.by_name("Redis sessions").detail.startswith("check stopped: ALLOWED_HOSTS is empty")
    assert report.by_name("Production settings") is not None and report.exit_code == 1


def test_manage_run_prints_the_startup_warnings(monkeypatch, capsys):
    calls: list[list[str]] = []
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: calls.append(list(args)) or 0)
    monkeypatch.setattr(manage, "load_settings", lambda: prod(**INTRANET, cookie_secure=False, force_https=False,
                                                              redis_url="redis://127.0.0.1:6379/5"))
    assert manage.main(["run", "--workers", "2", "--port", "12399"]) == 0
    out = capsys.readouterr().out
    assert "[manage] warning: COOKIE_SECURE=false in production" in out
    assert "--proxy-headers" in calls[-1]  # production trusts proxy headers (only from TRUSTED_PROXIES) by default
    # ... and uvicorn gets exactly that list (never its own $FORWARDED_ALLOW_IPS fallback, often '*')
    assert calls[-1][calls[-1].index("--forwarded-allow-ips") + 1] == "127.0.0.1,::1"
    monkeypatch.setattr(manage, "load_settings", lambda: prod(**SITE,
                                                              redis_url="redis://127.0.0.1:6379/5"))
    assert manage.main(["run", "--workers", "2", "--port", "12399"]) == 0
    assert "warning" not in capsys.readouterr().out


def test_env_example_leaves_the_production_defaults_alone(tmp_path):
    example = ROOT / ".env.example"
    values = dotenv_values(example)
    for key in ("COOKIE_SECURE", "FORCE_HTTPS", "TRUST_PROXY_HEADERS", "ALLOWED_HOSTS"):
        assert key not in values, f"{key} pinned in .env.example would switch the production default off"
    text = example.read_text(encoding="utf-8")
    for key in ("COOKIE_SECURE", "FORCE_HTTPS", "TRUST_PROXY_HEADERS", "ALLOWED_HOSTS", "AI_SIMULATED_ALLOWED",
                "AI_SIM_FIRST_TOKEN_MS", "AI_SIM_TOKENS_PER_S", "AI_SIM_FAILURE_RATE", "AI_SIM_MAX_CONCURRENCY",
                "AI_SIM_TTS", "AI_SIM_TTS_MS"):
        assert f"# {key}=" in text, key  # documented, commented out
    # a .env made from the example (secrets filled in) gets the secure defaults once ENVIRONMENT=production
    env = tmp_path / ".env"
    env.write_text(text.replace("SECRET_KEY=change-me-run-setup-to-generate-a-64-char-secret", f"SECRET_KEY={SECRET}")
                   .replace("DATA_ENCRYPTION_KEY=", f"DATA_ENCRYPTION_KEY={FERNET}", 1)
                   .replace("ID_HASH_PEPPER=", f"ID_HASH_PEPPER={PEPPER}", 1), encoding="utf-8")
    s = build_settings(_env_file=str(env), environment="production")
    assert s.cookie_secure and s.force_https and s.trust_proxy_headers
    assert s.ai_provider_order == ["gateway", "docker", "anthropic"]  # never the simulated provider by default
    # ... and ALLOWED_HOSTS counts as not configured: a warning on a loopback HOST, a refusal on the network
    assert any(w.startswith("ALLOWED_HOSTS is not set") for w in s.production_warnings())
    with pytest.raises(ConfigError) as err:
        build_settings(_env_file=str(env), environment="production", host="0.0.0.0")
    assert err.value.problems[0].startswith("ALLOWED_HOSTS allows only localhost")


def test_readme_documents_production_mode():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Deploying to production" in readme
    section = readme.split("## Deploying to production", 1)[1].split("\n## ", 1)[0]
    for needle in ("COOKIE_SECURE", "FORCE_HTTPS", "TRUST_PROXY_HEADERS", "ALLOWED_HOSTS", "SECRET_KEY",
                   "DATA_ENCRYPTION_KEY", "ID_HASH_PEPPER", "CORS_ORIGINS", "TRUSTED_PROXIES", "SUPERADMIN_PASSWORD",
                   "AI_SIMULATED_ALLOWED", "AI_SIM_FIRST_TOKEN_MS", "AI_SIM_TOKENS_PER_S", "AI_SIM_FAILURE_RATE",
                   "AI_SIM_MAX_CONCURRENCY", "AI_SIM_TTS_MS", "exit status 3", "manage.py check"):
        assert needle in section, needle


def test_the_load_test_environment_starts_cleanly():
    """loadtest/ltenv.py sets the production settings explicitly; it keeps working (read, never edited here)."""
    ltenv = _load("ltenv", ROOT / "loadtest" / "ltenv.py")
    try:
        env = ltenv.server_env("off")
    except SystemExit as exc:  # no .env / runtime passwords on this machine
        pytest.skip(f"load-test environment unavailable: {exc}")
    if not (ROOT / ".env").is_file():
        pytest.skip("the load test takes its secrets from .env")
    s = build_settings(**{key.lower(): value for key, value in env.items()})
    assert s.is_production and s.cookie_secure and s.force_https and s.trust_proxy_headers
    assert s.production_warnings() == [] and s.allowed_hosts == ["127.0.0.1", "localhost"]
