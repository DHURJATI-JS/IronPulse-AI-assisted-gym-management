"""The security flush never loses what it took off the queues (hunts F17, F37, F49 - app/security/bans.py).

* F17 / F37: one request with ``%00`` in its path (uvicorn decodes it into a literal NUL) made the batch INSERT fail
  ("invalid byte sequence for encoding UTF8: 0x00"): every event and every ban decision of those 2 seconds - other
  attackers' bans, the sender's own - was gone, and the security centre never saw the attack.
* F49: a database hiccup at flush time (statement timeout, lost connection) discarded the batch the same way: the ban
  lived only in one worker's memory, the other workers never synced it and a restart forgot it.
* The registry entry became "confirmed" (and the log said "banned") before the COMMIT, so the next full ban sync dropped
  a ban that was never stored.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text

from app import db as dbmod
from app.core.utils import utcnow
from app.models import IpBan, SecurityEvent
from app.models.enums import ThreatCategory
from app.security import bans
from app.security.bans import ban_registry, flush_security_queue, security_events
from tests.conftest import make_client

PASSWD = "/?f=/etc/passwd"  # severity 5: banned at once


async def _rows(model, ip: str) -> list:
    async with dbmod.get_sessionmaker()() as s:
        return list((await s.execute(select(model).where(model.ip == ip))).scalars().all())


async def test_a_nul_byte_request_no_longer_erases_other_attackers_bans_and_events(db):
    """The hunters' repro_nul_flush.py: an attacker's probe and anyone's ``%00`` request in the same 2-second window."""
    attacker, nul_sender = "203.0.113.167", "203.0.113.46"
    async with make_client(attacker) as a, make_client(nul_sender) as n:
        assert (await a.get(PASSWD)).status_code == 403
        assert (await n.get("/gyms/forge-and-flow%00/branches")).status_code == 403
    assert any("\x00" not in e.path and "\\x00" in e.path for e in security_events.events if e.ip == nul_sender)
    await flush_security_queue(db)
    await db.commit()
    assert len(await _rows(IpBan, attacker)) == 1
    assert len(await _rows(SecurityEvent, attacker)) >= 1
    stored = await _rows(SecurityEvent, nul_sender)
    assert stored and all(r.path == "/gyms/forge-and-flow\\x00/branches" for r in stored)  # kept, visibly escaped
    assert not ban_registry.get(attacker).provisional  # confirmed - the database has it


async def test_an_attacker_cannot_erase_its_own_ban_with_a_nul_request(db):
    """repro_nul_self.py: one /x%00y request of its own first, then the probe - 0 ip_bans rows and 0 events before."""
    ip = "203.0.113.168"
    async with make_client(ip) as a:
        assert (await a.get("/gyms/x%00y")).status_code == 403
        assert (await a.get(PASSWD)).status_code == 403
    await flush_security_queue(db)
    await db.commit()
    assert len(await _rows(IpBan, ip)) == 1 and len(await _rows(SecurityEvent, ip)) >= 2


async def test_the_worker_task_persists_the_window_with_a_nul_request():
    """The same through the background runner's own task (one session_scope for the security flush and the AI call
    logs, as in production)."""
    from app.services.worker import BackgroundRunner

    attacker, nul_sender = "203.0.113.169", "203.0.113.47"
    async with make_client(attacker) as a, make_client(nul_sender) as n:
        await a.get(PASSWD)
        await n.get("/index.php%00.jpg")
    await BackgroundRunner._security()
    assert len(await _rows(IpBan, attacker)) == 1 and await _rows(SecurityEvent, nul_sender)
    assert not security_events.events and not security_events.bans


async def test_a_value_the_database_still_refuses_costs_only_its_own_row(db):
    """Last line of defence: an event that skipped ``storable`` (queued directly) is dropped alone; the rest of the batch,
    the ban decisions and the caller's transaction go on."""
    good, bad = "198.51.100.60", "198.51.100.61"
    security_events.record(good, ThreatCategory.PROBE, 3, "probe for /.env", method="GET", path="/.env")
    security_events.events.append(bans.PendingEvent(bad, ThreatCategory.TRAVERSAL, 4, "raw\x00detail", "GET", "/a\x00b",
                                                    None, "blocked", None, utcnow()))
    security_events.record(good, ThreatCategory.XSS, 3, "<script> tag in query", method="GET", path="/gyms")
    bans.request_ban(good, 4, "probe", None)
    written = await flush_security_queue(db)
    await db.commit()
    assert written == 3  # 2 events + 1 ban
    assert len(await _rows(SecurityEvent, good)) == 2 and await _rows(SecurityEvent, bad) == []
    assert len(await _rows(IpBan, good)) == 1


async def test_a_flush_that_is_not_committed_goes_back_to_the_queue_and_is_written_next_time():
    """F49: the caller's transaction fails after the flush (a statement timeout, a lost connection, a failure later in
    the same transaction): before, the popped batch was simply gone."""
    ip = "198.51.100.62"
    security_events.record(ip, ThreatCategory.TRAVERSAL, 5, "system file access in query", method="GET", path="/")
    bans.request_ban(ip, 5, "system file access in query", None)
    with pytest.raises(RuntimeError):
        async with dbmod.session_scope() as s:
            await flush_security_queue(s)
            assert ban_registry.get(ip).provisional  # not confirmed before the COMMIT ...
            raise RuntimeError("the transaction fails after the flush")
    assert [e.ip for e in security_events.events] == [ip] and [b.ip for b in security_events.bans] == [ip]
    assert ban_registry.get(ip).provisional  # ... nor after a rollback: still blocked here, still queued
    assert await _rows(IpBan, ip) == []
    async with dbmod.session_scope() as s:  # the next flush
        assert await flush_security_queue(s) == 2
    assert len(await _rows(IpBan, ip)) == 1 and len(await _rows(SecurityEvent, ip)) == 1
    assert not ban_registry.get(ip).provisional
    assert not security_events.events and not security_events.bans


async def test_a_statement_timeout_during_the_flush_keeps_the_ban(monkeypatch):
    """The hunter's r03 repro: the database is slow / locked while the flush runs (statement_timeout) - the ban must be
    persisted once the database is back, not lost."""
    ip = "198.51.100.63"
    bans.request_ban(ip, 5, "system file access in query", None)
    security_events.record(ip, ThreatCategory.TRAVERSAL, 5, "system file access in query", method="GET", path="/")
    maker = dbmod.get_sessionmaker()
    async with maker() as holder:  # another worker's flush holds the ban-flush lock for a while
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": bans.BAN_FLUSH_LOCK_KEY})
        with pytest.raises(Exception) as caught:
            async with dbmod.session_scope() as s:
                await s.execute(text("SET LOCAL statement_timeout = '300ms'"))
                await flush_security_queue(s)
        assert "statement timeout" in str(caught.value) or "QueryCanceled" in repr(caught.value)
        await holder.rollback()
    assert [b.ip for b in security_events.bans] == [ip] and [e.ip for e in security_events.events] == [ip]
    async with dbmod.session_scope() as s:
        await flush_security_queue(s)
    assert len(await _rows(IpBan, ip)) == 1 and len(await _rows(SecurityEvent, ip)) == 1


async def test_a_failure_before_any_statement_puts_the_batch_back(monkeypatch):
    ip = "198.51.100.64"
    bans.request_ban(ip, 3, "probe", None)

    async def broken(*args, **kwargs):
        raise RuntimeError("bug before the first statement")

    monkeypatch.setattr(bans, "_finalise_bans", broken)
    async with dbmod.get_sessionmaker()() as s:
        with pytest.raises(RuntimeError):
            await flush_security_queue(s)
    assert [b.ip for b in security_events.bans] == [ip]


def test_every_queued_text_is_storable():
    assert bans.storable("/a\x00b", 512) == "/a\\x00b"
    assert bans.storable("x\ud800y", 10) == "x\\ud800y"
    assert bans.storable(None, 5) == "" and len(bans.storable("\x00" * 600, 512)) == 512
    security_events.record("198.51.100.65", ThreatCategory.PROBE, 3, "probe for /x\x00", method="G\x00T",
                           path="/x\x00", user_agent="ua\x00")
    e = security_events.events[-1]
    assert "\x00" not in e.detail + e.method + e.path + (e.user_agent or "")
    bans.request_ban("198.51.100.65", 3, "probe for /x\x00", None)
    assert "\x00" not in security_events.bans[-1].reason and "\x00" not in ban_registry.get("198.51.100.65").reason


async def test_the_flush_writes_concurrently_without_losing_or_doubling_anything():
    """Two flushes at once (the worker loop and a super-admin's "Run triage now") each write what they took."""
    for i in range(40):
        security_events.record(f"198.51.101.{i}", ThreatCategory.PROBE, 3, "probe", method="GET", path="/.env")
    for i in range(10):
        bans.request_ban(f"198.51.102.{i}", 3, "probe", None)

    async def one() -> int:
        async with dbmod.session_scope() as s:
            return await flush_security_queue(s)

    total = sum(await asyncio.gather(one(), one()))
    assert total == 50
    async with dbmod.get_sessionmaker()() as s:
        assert len((await s.execute(select(SecurityEvent.id))).all()) == 40
        assert len((await s.execute(select(IpBan.id))).all()) == 10

