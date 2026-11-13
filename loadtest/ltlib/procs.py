"""PID bookkeeping of the processes loadtest/run_server.sh starts - identity = PID + start time + command line.

    .venv-loadtest/bin/python loadtest/ltlib/procs.py record NAME PID          # right after spawning it
    .venv-loadtest/bin/python loadtest/ltlib/procs.py record-children NAME MASTER_PID   # the uvicorn workers
    .venv-loadtest/bin/python loadtest/ltlib/procs.py check NAME               # exit 0 + PID = ours and running
    .venv-loadtest/bin/python loadtest/ltlib/procs.py stop NAME [--timeout 30]
    .venv-loadtest/bin/python loadtest/ltlib/procs.py sweep                    # drop stale records / PID files
    .venv-loadtest/bin/python loadtest/ltlib/procs.py status

A PID file left behind by a crash or a reboot points at a process that is gone - or, after PID reuse, at an unrelated
program. A process is only ever signalled when its PID, its start time AND its command line match what was recorded
when this kit started it (a legacy PID-only file from an older kit: the command line must match the kit's pattern).
Anything else is reported as stale and its record / PID file removed - never signalled.

Names: server (uvicorn master), workers (its children), monitor, smtp. Registry: loadtest/.procs.json; the plain
PID files loadtest/.server.pid / .monitor.pid / .smtp.pid are kept for people (and older kits).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psutil

LT = Path(__file__).resolve().parent.parent
REGISTRY = LT / ".procs.json"
# name -> (plain PID file, a command-line fragment only that process has)
KNOWN: dict[str, tuple[Path | None, str]] = {
    "server": (LT / ".server.pid", "uvicorn app.main:app --host 127.0.0.1 --port 12000"),
    "workers": (None, "spawn_main"),  # uvicorn's worker processes (multiprocessing spawn)
    "monitor": (LT / ".monitor.pid", "loadtest/monitor.py"),
    "smtp": (LT / ".smtp.pid", "loadtest/smtp_sink.py"),
}
START_TOLERANCE_S = 1.0


def say(msg: str) -> None:
    print(f"[lt-procs] {msg}", flush=True)


def load() -> dict[str, Any]:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(reg: dict[str, Any]) -> None:
    tmp = REGISTRY.with_name(REGISTRY.name + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def cmdline(p: psutil.Process) -> str:
    try:
        return " ".join(p.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return ""


def identity(pid: int) -> dict[str, Any] | None:
    try:
        p = psutil.Process(pid)
        return {"pid": pid, "create_time": p.create_time(), "cmdline": cmdline(p)[:400]}
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def why_not_ours(rec: dict[str, Any], pattern: str) -> str | None:
    """None when the recorded process is still the very same process; else why not (gone / another program)."""
    pid = int(rec.get("pid") or 0)
    if pid <= 0:
        return "no PID"
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return f"PID {pid} has exited"
        started = p.create_time()
        cmd = cmdline(p)
    except psutil.NoSuchProcess:
        return f"PID {pid} is not running"
    except (psutil.AccessDenied, psutil.ZombieProcess):
        return f"PID {pid} belongs to another user or has exited"
    if pattern not in cmd:
        return f"PID {pid} is another program now ({cmd[:90] or '?'})"
    want = rec.get("create_time")
    if want is not None and abs(float(want) - started) > START_TOLERANCE_S:
        return f"PID {pid} was started at {time.strftime('%H:%M:%S', time.localtime(started))}, not when this kit " \
               "started it (PID reuse)"
    return None


def legacy_record(name: str) -> dict[str, Any] | None:
    """A plain PID file without a registry entry (older kit, or the registry was lost): PID only, no start time."""
    pid_file = KNOWN[name][0]
    if pid_file is None or not pid_file.is_file():
        return None
    try:
        return {"pid": int(pid_file.read_text(encoding="utf-8").split()[0]), "create_time": None, "legacy": True}
    except (OSError, ValueError, IndexError):
        return {"pid": 0, "create_time": None, "legacy": True}


def forget(reg: dict[str, Any], name: str) -> None:
    reg.pop(name, None)
    pid_file = KNOWN[name][0]
    if pid_file is not None:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


def current(reg: dict[str, Any], name: str) -> dict[str, Any] | None:
    rec = reg.get(name)
    return rec if isinstance(rec, dict) else legacy_record(name)


# ------------------------------------------------------------------------------------------------ commands
def cmd_record(name: str, pid: int) -> int:
    reg = load()
    ident = identity(pid)
    if ident is None:
        say(f"cannot record {name}: PID {pid} is not running")
        return 1
    if KNOWN[name][1] not in ident["cmdline"]:
        say(f"cannot record {name}: PID {pid} does not look like it ({ident['cmdline'][:90]})")
        return 1
    reg[name] = ident
    save(reg)
    pid_file = KNOWN[name][0]
    if pid_file is not None:
        pid_file.write_text(f"{pid}\n", encoding="utf-8")
    return 0


def cmd_record_children(name: str, master: int) -> int:
    reg = load()
    try:
        kids = psutil.Process(master).children(recursive=False)
    except psutil.NoSuchProcess:
        kids = []
    out = []
    for k in kids:
        ident = identity(k.pid)
        if ident is not None and KNOWN[name][1] in ident["cmdline"]:
            out.append(ident)
    reg[name] = {"master": master, "procs": out}
    save(reg)
    print(" ".join(str(x["pid"]) for x in out))
    return 0


def cmd_check(name: str) -> int:
    reg = load()
    rec = current(reg, name)
    if rec is None:
        return 1
    if why_not_ours(rec, KNOWN[name][1]) is None:
        print(rec["pid"])
        return 0
    return 1


def stop_one(rec: dict[str, Any], label: str, pattern: str, timeout: float) -> str:
    why = why_not_ours(rec, pattern)
    if why is not None:
        return f"{label}: stale record ({why}) - nothing signalled"
    pid = int(rec["pid"])
    p = psutil.Process(pid)
    try:
        p.terminate()
        try:
            p.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            if why_not_ours(rec, pattern) is None:  # still the same process: force it
                p.kill()
                p.wait(timeout=10)
                return f"{label} (PID {pid}) did not stop in {timeout:.0f} s - killed"
    except psutil.NoSuchProcess:
        pass
    return f"{label} (PID {pid}) stopped"


def cmd_stop(name: str, timeout: float) -> int:
    reg = load()
    if name == "workers":
        rec = reg.get("workers") or {}
        for w in rec.get("procs") or []:
            if why_not_ours(w, KNOWN["workers"][1]) is None:  # normally they exit with the master
                say(stop_one(w, "worker", KNOWN["workers"][1], 10))
        reg.pop("workers", None)
        save(reg)
        return 0
    rec = current(reg, name)
    if rec is None:
        say(f"{name}: not running (no record)")
        return 0
    say(stop_one(rec, name, KNOWN[name][1], timeout))
    forget(reg, name)
    save(reg)
    return 0


def cmd_sweep() -> int:
    """Before a start: drop the records of processes that are gone or are not ours any more (after a reboot the PIDs
    point at nothing - or at unrelated programs). Exit status 3 when the load-test server IS still running."""
    reg = load()
    running = []
    for name, (_pid_file, pattern) in KNOWN.items():
        if name == "workers":
            rec = reg.get("workers") or {}
            alive = [w for w in rec.get("procs") or [] if why_not_ours(w, pattern) is None]
            if rec and not alive:
                reg.pop("workers", None)
            continue
        rec = current(reg, name)
        if rec is None:
            continue
        why = why_not_ours(rec, pattern)
        if why is None:
            running.append(f"{name} PID {rec['pid']}")
            continue
        src = "PID file" if rec.get("legacy") else "record"
        say(f"stale {name} {src} removed: {why} - nothing signalled")
        forget(reg, name)
    save(reg)
    if running:
        print("running: " + ", ".join(running))
        return 3
    return 0


def cmd_status() -> int:
    reg = load()
    for name, (_pid_file, pattern) in KNOWN.items():
        if name == "workers":
            rec = reg.get("workers") or {}
            procs = rec.get("procs") or []
            alive = [str(w["pid"]) for w in procs if why_not_ours(w, pattern) is None]
            if procs:
                say(f"workers: {len(alive)} of {len(procs)} running ({' '.join(alive) or '-'})")
            continue
        rec = current(reg, name)
        if rec is None:
            say(f"{name}: not running")
            continue
        why = why_not_ours(rec, pattern)
        say(f"{name}: running (PID {rec['pid']})" if why is None else f"{name}: not running - stale record ({why})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("name", choices=[k for k in KNOWN if k != "workers"])
    r.add_argument("pid", type=int)
    rc = sub.add_parser("record-children")
    rc.add_argument("name", choices=["workers"])
    rc.add_argument("master", type=int)
    c = sub.add_parser("check")
    c.add_argument("name", choices=list(KNOWN))
    s = sub.add_parser("stop")
    s.add_argument("name", choices=list(KNOWN))
    s.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("sweep")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "record":
        return cmd_record(args.name, args.pid)
    if args.cmd == "record-children":
        return cmd_record_children(args.name, args.master)
    if args.cmd == "check":
        return cmd_check(args.name)
    if args.cmd == "stop":
        return cmd_stop(args.name, args.timeout)
    if args.cmd == "sweep":
        return cmd_sweep()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
