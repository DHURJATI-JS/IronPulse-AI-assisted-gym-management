"""loadtest/analyze.py - the whole-run verdict "abusers throttled" (``abuse_summary``).

The stress test of 2026-11-13 failed it on overload artefacts: one WebSocket flood whose connect timed out and six
load-shedding 503s of the identical-burst pattern, all in steps where the normal users were shed as well. Those are
now excused exactly like the junk senders' (reported separately, not counted); a pattern that was really never
throttled - no 429 / 403 at all, a WebSocket flood never closed with 4429 - and every other failure still fail it; the
abuser's own sign-in (setup) is never judged as abuse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

LOADTEST = Path(__file__).resolve().parent.parent / "loadtest"
T0 = 1_794_552_000.0
CALM, SHED = 0, 1  # the two steps below: [0, 100) s normal users fine, [100, 200) s normal users shed too


@pytest.fixture(scope="module")
def analyze() -> Any:
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location("loadtest_analyze_under_test", LOADTEST / "analyze.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # standard library only (+ loadtest/ltenv.py)
    sys.path[:] = before
    return module


def steps(**shed: int) -> list[dict[str, Any]]:
    calm = {"users": 700, "role": "rising", "start_s": 0.0, "end_s": 100.0, "normal_503": 0, "normal_transport": 0,
            "normal_ws_shed": 0}
    hot = {"users": 1600, "role": "peak", "start_s": 100.0, "end_s": 200.0, "normal_503": 135, "normal_transport": 0,
           "normal_ws_shed": 49, **shed}
    return [calm, hot]


def row(name: str, status: str = "", *, ok: bool = True, err: str = "", step: int = CALM, n: int = 0,
        rtype: str | None = None) -> dict[str, Any]:
    method = rtype or ("WS" if " WS " in f" {name} " or name.startswith("ABUSE WS") else name.split()[1])
    return {"t": T0 + 100.0 * step + 1 + n * 0.01, "type": method, "name": name, "status": status, "ok": ok,
            "err": err, "ms": 12.0}


def patterns(*rows: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault((r["type"], r["name"]), []).append(r)
    return out


BURST = "ABUSE GET /shop?q= (identical burst)"
FLOOD = "ABUSE WS /ws (flood)"
BRUTE = "ABUSE POST /auth/login (unknown account)"
OWN = "ABUSE POST /auth/login (own account)"


def throttled() -> list[dict[str, Any]]:
    """The patterns of a healthy run: each refused as designed."""
    return [row(BURST, "200"), row(BURST, "429", n=1), row(BURST, "403", n=2),
            row(FLOOD, ok=True), row(FLOOD, ok=True, n=1),
            row(BRUTE, "400"), row(BRUTE, "429", n=1)]


def test_overload_artefacts_in_load_shedding_steps_are_reported_not_counted(analyze):
    """The run of 2026-11-13: 6 x 503 of the burst and 1 WebSocket flood that timed out connecting, in the spike."""
    rs = throttled() + [row(BURST, "503", ok=False, err="503 server error", step=SHED, n=k) for k in range(6)] + [
        row(FLOOD, ok=False, err="Connection timed out", step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is True and out["problems"] == []
    assert len(out["shed"]) == 7 and {s["kind"] for s in out["shed"]} == {"503", "timeout"}
    assert "7 overload failures in load-shedding steps not counted (6 x 503, 1 x timeout)" in out["detail"]
    assert any("6 x 503 in `ABUSE GET /shop?q= (identical burst)` - the 1600-user step" in line
               for line in out["shed_lines"])
    by = {r[0]: r for r in out["rows"]}
    assert by[BURST][9:11] == [0, 6] and by[BURST][-1] == "refused"  # failures counted 0, overload 6
    assert by[FLOOD][9:11] == [0, 1] and by[FLOOD][-1] == "refused"


def test_a_1013_try_again_later_at_the_flood_handshake_is_overload_too(analyze):
    rs = throttled() + [row(FLOOD, ok=False, err="closed before hello: 1013 try again later - server overloaded",
                            step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is True and [s["kind"] for s in out["shed"]] == ["timeout"]


def test_the_same_failures_in_a_calm_step_still_fail(analyze):
    rs = throttled() + [row(BURST, "503", ok=False, err="503 server error", step=CALM, n=5),
                        row(FLOOD, ok=False, err="Connection timed out", step=CALM, n=5)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is False and out["shed"] == []
    assert any(p.startswith(f"`{BURST}`: 1 failed outside the load-shedding steps (503 x1)") for p in out["problems"])
    assert any(p.startswith(f"`{FLOOD}`: 1 failed outside") for p in out["problems"])
    assert "NOT THROTTLED / FAILED" in out["detail"]


def test_a_pattern_that_was_never_refused_fails_even_while_the_server_was_overloaded(analyze):
    rs = [row(BURST, "200", n=k) for k in range(20)] + [row(BURST, "503", ok=False, err="503", step=SHED)] + [
        row(FLOOD, ok=True), row(BRUTE, "429")]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is False
    assert out["problems"] == [f"`{BURST}`: never refused (no 429 / 403 at all)"]
    assert {r[0]: r[-1] for r in out["rows"]}[BURST] == "**not throttled**"


@pytest.mark.parametrize("err", ["flood not stopped (close code None)", "flood not stopped (close code 1006)",
                                 "closed before hello: 1011"])
def test_a_websocket_flood_that_was_not_stopped_fails_in_any_step(analyze, err):
    rs = throttled() + [row(FLOOD, ok=False, err=err, step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is False and out["shed"] == []
    assert out["problems"] == [f"`{FLOOD}`: 1 failed outside the load-shedding steps ({err} x1)"]


def test_a_websocket_flood_never_closed_with_4429_fails(analyze):
    rs = [row(BURST, "429"), row(BRUTE, "403"), row(FLOOD, ok=False, err="Connection timed out", step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is False
    assert out["problems"] == [f"`{FLOOD}`: never closed with 4429 (nor refused at the handshake)"]


def test_a_successful_wrong_password_sign_in_is_never_excused(analyze):
    rs = throttled() + [row(BRUTE, "303", ok=False, err="unexpected status 303", step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is False and out["shed"] == []


def test_the_abusers_own_sign_in_is_setup_and_never_counts_as_unthrottled_abuse(analyze):
    """'ABUSE POST /auth/login (own account)' (and the page it follows to, and the sign-in page fetched first) is the
    abuser signing in to its own account before a flood: 0 % refused is right, and even its failures are not abuse."""
    rs = throttled() + [row(OWN, "303", n=k) for k in range(25)] + [
        row(f"{OWN} -> GET /me", "200"), row("ABUSE GET /auth/login", "200"),
        row(OWN, "503", ok=False, err="503 server error", step=CALM, n=40)]
    out = analyze.abuse_summary(patterns(*rs), steps(), T0)
    assert out["ok"] is True and out["problems"] == []
    verdicts = {r[0]: r[-1] for r in out["rows"]}
    assert verdicts[OWN] == verdicts[f"{OWN} -> GET /me"] == verdicts["ABUSE GET /auth/login"] == "setup"
    assert out["setup_failures"] == 1 and "1 failures of setup rows (own sign-in), not judged" in out["detail"]
    assert analyze.abuse_setup(OWN) and not analyze.abuse_setup(BRUTE) and not analyze.abuse_setup(BURST)


def test_only_the_normal_users_shedding_makes_a_step_a_load_shedding_one(analyze):
    """An abuser's own transport errors never excuse themselves: the step counts as shed only when the normal users got
    a 503, a transport error or a socket turned away for overload."""
    calm_with_abuser_noise = steps(normal_503=0, normal_transport=0, normal_ws_shed=0)
    rs = throttled() + [row(BURST, "", ok=False, err="transport error: ReadTimeout()", step=SHED)]
    out = analyze.abuse_summary(patterns(*rs), calm_with_abuser_noise, T0)
    assert out["ok"] is False and out["shed"] == []
    for signal in ("normal_503", "normal_transport", "normal_ws_shed"):
        shed = analyze.abuse_summary(patterns(*rs), steps(**{"normal_503": 0, "normal_ws_shed": 0, signal: 3}), T0)
        assert shed["ok"] is True and len(shed["shed"]) == 1, signal


def test_no_attack_pattern_at_all_is_not_a_verdict(analyze):
    out = analyze.abuse_summary(patterns(row(OWN, "303"), row("ABUSE GET /auth/login", "200")), steps(), T0)
    assert out["ok"] is None
