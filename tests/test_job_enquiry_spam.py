"""Anonymous job enquiries (POST /jobs/{id}/enquire): deterministic spam protection - no AI.

* honeypot ``website`` filled -> a pretend success: nothing stored, nobody notified;
* a signed timestamp token in the form (issued with the job page, bound to the posting): missing, forged, another
  posting's, expired or from the future, or sent within ENQUIRY_MIN_FILL_SECONDS of the page -> "send again" (400);
* links (at most ENQUIRY_MAX_LINKS in the message, none in the name) and the length (20-4,000) are capped (400);
* one enquiry per network address and posting per ENQUIRY_COOLDOWN_SECONDS (429 + Retry-After);
* the same message (case, spacing, invisible characters and compatibility forms aside, in any script - compared by a
  key computed in Python, ``enquiry_message_key``) to the same posting within ENQUIRY_DUPLICATE_HOURS -> 409;
* both checks run under a per-posting advisory lock: two identical enquiries at the same moment store one;
* every refusal shows the form again with the visitor's details and a friendly reason, and stores (not even the
  résumé), notifies and e-mails nothing - while a first enquiry from a filled-in form always goes through;
* the checks stay cheap whatever the input or the history: links are counted in linear time, and the cooldown /
  duplicate check reads only the posting's last 24 h (ix_job_enquiries_job_recent), never all its enquiries.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
import time
import unicodedata
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text

from app import db as dbmod
from app.models import EmailOutbox, Gym, JobEnquiry, Notification
from app.routers import public
from app.services import storage
from tests.conftest import post_form
from tests.test_public import make_job, pdf_bytes, published_gym

FIELD = public.ENQUIRY_TOKEN_FIELD
MESSAGE = "Certified strength coach, four years on the gym floor, free on weekday mornings."


def token(job, age: float = 60.0) -> str:
    """The token the job page issued ``age`` seconds ago."""
    return public.enquiry_form_token(job.id, issued=time.time() - age)


def form(job, **fields) -> dict:
    data = {"name": "Asha Rao", "email": "asha@example.com", "message": MESSAGE, FIELD: token(job)}
    data.update(fields)
    return data


def url(job) -> str:
    return f"/jobs/{job.id}/enquire"


async def counts(db) -> tuple[int, int, int]:
    """(enquiries, notifications, e-mails) stored."""
    return (await db.scalar(select(func.count(JobEnquiry.id))), await db.scalar(select(func.count(Notification.id))),
            await db.scalar(select(func.count(EmailOutbox.id))))


def shown_token(html: str) -> str | None:
    m = re.search(rf'name="{FIELD}" value="([^"]+)"', html)
    return m.group(1) if m else None


def notice(html: str) -> str:
    """The refusal shown above the form."""
    m = re.search(r"<div class=\"mt-5\" data-enquiry-refusal>(.*?)</div>\s*</div>", html, re.DOTALL)
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""


def clock(monkeypatch, seconds_from_now: float) -> None:
    """The enquiry route's clock (only its own ``time`` module reference) runs ``seconds_from_now`` ahead."""
    later = time.time() + seconds_from_now
    monkeypatch.setattr(public, "time", SimpleNamespace(time=lambda: later))


async def gym_of(db, job) -> Gym:
    return await db.get(Gym, job.gym_id)


@pytest.fixture
async def job(db):
    _, gym, _ = await published_gym(db)
    return await make_job(db, gym)


# ------------------------------------------------------------------ a first enquiry always goes through
async def test_a_first_enquiry_from_the_page_goes_through(client, db, job, monkeypatch):
    other = await make_job(db, await gym_of(db, job), title="Front Desk Lead")
    page = (await client.get(f"/jobs/{job.id}")).text
    issued = shown_token(page)
    assert issued and public.enquiry_form_age(issued, job.id) is not None  # signed for this posting ...
    assert public.enquiry_form_age(issued, other.id) is None  # ... and for no other
    assert 'name="website"' in page and "at most 2 links" in page
    clock(monkeypatch, 30)  # the visitor takes half a minute to fill it in
    stored: list[str] = []
    real_save = storage.save_private_file

    async def save(*args, **kwargs):
        result = await real_save(*args, **kwargs)
        stored.append(result[0].key)
        return result

    monkeypatch.setattr(storage, "save_private_file", save)
    r = await post_form(client, url(job), {"name": "Asha Rao", "email": "asha@example.com", "message": MESSAGE,
                                           "website": "", FIELD: issued},
                        files={"resume": ("Asha CV.pdf", pdf_bytes(), "application/pdf")})
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.id}?sent=1#enquire"
    assert await counts(db) == (1, 1, 2) and len(stored) == 1
    assert "Enquiry sent" in (await client.get(r.headers["location"])).text
    # the same visitor (same address, a minute later) enquires about another posting of the gym: no cooldown there
    r = await post_form(client, url(other), form(other, message="I ran the front desk of a busy studio for 3 years."))
    assert r.status_code == 303 and await counts(db) == (2, 2, 4)


# ------------------------------------------------------------------ the form token
async def test_a_form_sent_too_soon_is_asked_again_then_goes_through(client, db, job, monkeypatch):
    issued = shown_token((await client.get(f"/jobs/{job.id}")).text)
    r = await post_form(client, url(job), form(job, **{FIELD: issued}))  # within a second of the page: a bot's speed
    assert r.status_code == 400 and notice(r.text).startswith("That was quick!")
    assert 'value="Asha Rao"' in r.text and MESSAGE in r.text  # the details are kept
    assert shown_token(r.text) == issued  # the same token: the time already spent filling it in counts
    assert await counts(db) == (0, 0, 0)
    clock(monkeypatch, public.ENQUIRY_MIN_FILL_SECONDS + 1)
    r = await post_form(client, url(job), form(job, **{FIELD: issued}))
    assert r.status_code == 303 and await counts(db) == (1, 1, 2)


@pytest.mark.parametrize("case", ["missing", "forged", "another posting's", "expired", "from the future", "junk"])
async def test_a_form_without_a_valid_token_is_asked_again(client, db, job, monkeypatch, case):
    other = await make_job(db, await gym_of(db, job), title="Other Role")
    raw = {
        "missing": None,
        "forged": f"{int(time.time()) - 60}.{'0' * 32}",
        "another posting's": token(other),
        "expired": token(job, age=public.ENQUIRY_FORM_MAX_AGE + 60),
        "from the future": token(job, age=-3600),
        "junk": "²³.not-a-signature",
    }[case]
    data = form(job)
    if raw is None:
        data.pop(FIELD)
    else:
        data[FIELD] = raw
    r = await post_form(client, url(job), data)
    assert r.status_code == 400 and await counts(db) == (0, 0, 0)
    title = "This form has expired" if case == "expired" else "Please send your enquiry again"
    assert notice(r.text).startswith(title) and 'value="asha@example.com"' in r.text
    fresh = shown_token(r.text)
    assert fresh != raw and public.enquiry_form_age(fresh, job.id) is not None  # a new token for this posting
    clock(monkeypatch, public.ENQUIRY_MIN_FILL_SECONDS + 1)
    assert (await post_form(client, url(job), {**data, FIELD: fresh})).status_code == 303  # pressing Send again works


async def test_the_honeypot_is_a_silent_pretend_success(client, db, job):
    r = await post_form(client, url(job), form(job, website="http://spam.example"))
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.id}?sent=1#enquire"
    assert await counts(db) == (0, 0, 0)


# ------------------------------------------------------------------ links and length
async def test_links_and_length_are_capped(client, db, job):
    two = "Portfolio at https://example.com/asha and www.linkedin.com/in/asha-rao - mail me at asha@gmail.com."
    r = await post_form(client, url(job), form(job, message=two + " Also t.me/asha_coach for videos."))
    assert r.status_code == 400 and "at most 2 links (it has 3)" in r.text and "PDF instead" in r.text
    r = await post_form(client, url(job), form(job, name="Cheap pills www.pills.example"))
    assert r.status_code == 400 and "just your name (no links)" in r.text
    r = await post_form(client, url(job), form(job, message="x" * (public.ENQUIRY_MAX_CHARS + 1)))
    assert r.status_code == 400 and "under 4,000 characters" in r.text
    assert await counts(db) == (0, 0, 0)
    assert public.count_links(two) == 2  # an e-mail address is not a link
    assert (await post_form(client, url(job), form(job, message=two))).status_code == 303  # two links are fine


# ------------------------------------------------------------------ cooldown and duplicates
async def test_one_enquiry_per_address_and_posting_per_cooldown(db, job, anon_client_factory):
    other = await make_job(db, await gym_of(db, job), title="Night Manager")
    a, b = anon_client_factory("198.51.100.7"), anon_client_factory("198.51.100.8")
    assert (await post_form(a, url(job), form(job))).status_code == 303
    follow_up = "One more thing: I also hold a current first-aid certificate."
    r = await post_form(a, url(job), form(job, message=follow_up))
    assert r.status_code == 429 and 290 <= int(r.headers["retry-after"]) <= public.ENQUIRY_COOLDOWN_SECONDS
    assert notice(r.text).startswith("You've just sent an enquiry for this job") and "5 minutes" in notice(r.text)
    assert follow_up in r.text and await counts(db) == (1, 1, 2)
    r = await post_form(b, url(job), form(job, name="Ravi Kumar", message="Kettlebell coach, 2 years, evenings free."))
    assert r.status_code == 303  # another address
    r = await post_form(a, url(other), form(other, message="Managed a 24-hour gym's night shift for two years."))
    assert r.status_code == 303  # another posting
    await db.execute(text("UPDATE job_enquiries SET created_at = created_at - make_interval(secs => :s)"),
                     {"s": public.ENQUIRY_COOLDOWN_SECONDS + 1})
    await db.commit()
    assert (await post_form(a, url(job), form(job, message=follow_up))).status_code == 303  # the cooldown is over
    assert await counts(db) == (4, 4, 8)


async def test_the_same_message_to_the_same_posting_is_refused_for_24_hours(db, job, anon_client_factory):
    other = await make_job(db, await gym_of(db, job), title="Yoga Coach")
    a, b, c = (anon_client_factory(f"198.51.100.{i}") for i in (31, 32, 33))
    assert (await post_form(a, url(job), form(job))).status_code == 303
    same = "  " + MESSAGE.upper().replace(" ", "   ") + "\n"  # case and spacing aside, the same text
    r = await post_form(b, url(job), form(job, name="Copy Cat", email="copy@example.com", message=same))
    assert r.status_code == 409 and notice(r.text).startswith("This message was already sent")
    assert await counts(db) == (1, 1, 2)
    assert (await post_form(b, url(other), form(other, message=MESSAGE))).status_code == 303  # another posting
    await db.execute(text("UPDATE job_enquiries SET created_at = created_at - interval '25 hours' WHERE job_id = :j"),
                     {"j": job.id})
    await db.commit()
    assert (await post_form(c, url(job), form(job, message=MESSAGE))).status_code == 303  # a day later
    assert await counts(db) == (3, 3, 6)


async def test_a_message_that_only_looks_the_same_is_still_the_same_message(db, job, anon_client_factory):
    """F18: the duplicate check compared messages in SQL, where the cluster's C locale makes ``\\s``, btrim() and lower()
    ASCII-only. The same text with no-break / em / ideographic spaces, a zero-width character, full-width letters,
    decomposed accents or non-ASCII capitals got through (303) and reached the gym owner again, looking identical. Each
    enquiry now stores a key of the text as the owner reads it (``enquiry_message_key``: NFKC, casefold, spacing and
    invisible characters left out), and the check compares that key. A visible edit is still a new message."""
    french = "Entraîneur certifié, quatre ans d'expérience en salle, libre le matin."
    assert (await post_form(anon_client_factory("198.51.100.50"), url(job), form(job))).status_code == 303
    r = await post_form(anon_client_factory("198.51.100.51"), url(job), form(job, name="Élodie Roux", message=french))
    assert r.status_code == 303 and await counts(db) == (2, 2, 4)
    stored = set((await db.execute(select(JobEnquiry.message_key))).scalars())
    assert stored == {public.enquiry_message_key(MESSAGE), public.enquiry_message_key(french)}
    looks_the_same = {
        "no-break spaces": MESSAGE.replace(" ", "\u00a0"),
        "em spaces": MESSAGE.replace(" ", "\u2003"),
        "narrow no-break and ideographic spaces": MESSAGE.replace(" ", "\u202f\u3000"),
        "a zero-width space in a word": MESSAGE.replace("coach", "co\u200bach"),
        "a BOM, a soft hyphen, a word joiner": "\ufeff" + MESSAGE.replace("strength", "str\u00adength\u2060"),
        "Hangul fillers for spaces": MESSAGE.replace(" ", "\u3164"),
        "a variation selector, a grapheme joiner": MESSAGE.replace(",", ",\ufe0f\u034f"),
        "full-width letters": MESSAGE.replace("Certified", "Ｃｅｒｔｉｆｉｅｄ"),
        "decomposed accents": unicodedata.normalize("NFD", french),
        "non-ASCII capitals": french.upper(),
    }
    statuses = {}
    for i, (label, variant) in enumerate(looks_the_same.items()):
        r = await post_form(anon_client_factory(f"198.51.100.{60 + i}"), url(job),
                            form(job, name="Copy Cat", email=f"copy{i}@example.com", message=variant))
        statuses[label] = r.status_code
        if r.status_code == 409:
            assert notice(r.text).startswith("This message was already sent") and "Copy Cat" in r.text
    assert statuses == dict.fromkeys(looks_the_same, 409)
    assert await counts(db) == (2, 2, 4)  # nothing stored, notified or e-mailed
    edited = MESSAGE.replace("four", "five")  # a visible edit: a new message
    assert (await post_form(anon_client_factory("198.51.100.90"), url(job), form(job, message=edited))).status_code == 303
    assert await counts(db) == (3, 3, 6)


async def test_enquiries_stored_before_the_message_key_still_count(db, job, anon_client_factory):
    """Enquiries stored by the previous version have no message_key (NULL): for the 24 hours they still matter they are
    compared as before - case and ASCII spacing aside."""
    await db.execute(text("INSERT INTO job_enquiries (job_id, name, email, message, status, ip) "
                          "VALUES (:job, 'Asha Rao', 'asha@example.com', :message, 'new', '198.51.100.20')"),
                     {"job": job.id, "message": MESSAGE})
    await db.commit()
    same = "  " + MESSAGE.upper().replace(" ", "   ") + "\n"
    r = await post_form(anon_client_factory("198.51.100.21"), url(job), form(job, message=same))
    assert r.status_code == 409 and notice(r.text).startswith("This message was already sent")
    assert await counts(db) == (1, 0, 0)


async def test_two_identical_enquiries_at_the_same_moment_store_one(db, job, anon_client_factory, monkeypatch):
    """The checks run under a per-posting advisory lock taken before them: the second request waits until the first
    COMMITs, then sees its enquiry. (Without the lock both would pass the check before either committed - the notify
    step is slowed down here to hold the first transaction open.)"""
    real_notify = public.notify

    async def slow_notify(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_notify(*args, **kwargs)

    monkeypatch.setattr(public, "notify", slow_notify)
    a, b = anon_client_factory("198.51.100.41"), anon_client_factory("198.51.100.42")
    first, second = await asyncio.gather(post_form(a, url(job), form(job)),
                                         post_form(b, url(job), form(job, email="twin@example.com")))
    assert sorted([first.status_code, second.status_code]) == [303, 409]
    assert await counts(db) == (1, 1, 2)


async def test_refusals_store_no_resume(client, db, job, monkeypatch):
    saved: list[object] = []

    async def save(*args, **kwargs):
        saved.append(args)
        raise AssertionError("a refused enquiry must not store its résumé")

    monkeypatch.setattr(storage, "save_private_file", save)
    files = {"resume": ("cv.pdf", pdf_bytes(), "application/pdf")}
    assert (await post_form(client, url(job), form(job, **{FIELD: token(job, age=0)}), files=files)).status_code == 400
    r = await post_form(client, url(job), form(job, message="Visit https://a.example https://b.example https://c.example"),
                        files=files)
    assert r.status_code == 400 and "Please choose the file again." in r.text
    assert saved == [] and await counts(db) == (0, 0, 0)


# ------------------------------------------------------------------ the checks stay cheap
def test_links_are_counted_in_linear_time_on_crafted_messages():
    """count_links runs on anonymous messages of up to 4,000 characters, on the event loop. An unanchored
    ``(label.)+tld/`` pattern rescanned the rest of a run like "a.a.a.a..." from every letter: quadratic, ~0.1 s per
    message (and it ran twice on a refused one). Each crafted message now takes well under a millisecond."""
    for crafted in ("a." * 2000, "a-" * 2000, "a.a-" * 1000, "1.a" * 1333, "9." * 1999 + "aa", "ab." * 1333):
        timings = []
        for _ in range(3):  # the best of three: a busy machine can't fail it
            start = time.perf_counter()
            assert public.count_links(crafted) == 0
            timings.append(time.perf_counter() - start)
        assert min(timings) < 0.02, (crafted[:12], min(timings))
    assert public.count_links("see (example.com/x), my-site.co.uk/cv and sub.example.com/y") == 3  # still links


async def test_the_check_reads_only_the_postings_last_24_hours(client, db, job):
    """20,000 enquiries from before yesterday + 5 recent ones: the cooldown / duplicate check reads the recent ones only,
    in index order (ix_job_enquiries_job_recent: job_id, created_at DESC, id DESC) - no scan of the posting's whole
    history, no sort."""
    other = await make_job(db, await gym_of(db, job), title="Busy Role")
    await db.execute(text(
        "INSERT INTO job_enquiries (job_id, name, email, message, status, ip, created_at) "
        "SELECT CASE WHEN g % 10 = 0 THEN CAST(:other AS bigint) ELSE CAST(:job AS bigint) END, 'Old ' || g, 'old' || g || '@example.com', "
        "'An enquiry from long ago, number ' || g, 'new', '10.0.' || (g % 250) || '.' || (g % 200), "
        "now() - interval '2 days' - make_interval(secs => g) FROM generate_series(1, 22000) AS g"),
        {"job": job.id, "other": other.id})
    await db.execute(text(
        "INSERT INTO job_enquiries (job_id, name, email, message, status, ip, created_at) "
        "SELECT :job, 'New ' || g, 'new' || g || '@example.com', 'A recent enquiry, number ' || g, 'new', "
        "'192.0.2.' || g, now() - make_interval(mins => g) FROM generate_series(1, 5) AS g"), {"job": job.id})
    await db.commit()
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM ANALYZE job_enquiries")

    captured: list[tuple[str, object]] = []

    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        captured.append((statement, parameters))

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", grab)
    try:
        r = await post_form(client, url(job), form(job))
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert r.status_code == 303
    statement, params = next((s, p) for s, p in captured if "recent_enquiries" in s)
    async with dbmod.get_engine().connect() as conn:
        plan = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)).scalar()
    plan = (json.loads(plan) if isinstance(plan, str) else plan)[0]["Plan"]

    def nodes(node: dict):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    every = list(nodes(plan))
    scans = [n for n in every if n.get("Relation Name") == "job_enquiries"]
    assert [n.get("Index Name") for n in scans] == ["ix_job_enquiries_job_recent"], [
        (n["Node Type"], n.get("Index Name")) for n in scans]
    assert sum(n["Actual Rows"] * n.get("Actual Loops", 1) for n in scans) == 6  # the 5 + the one just sent
    assert not [n for n in every if n["Node Type"] in ("Sort", "Incremental Sort")]


async def test_the_enquiry_index_replaces_the_single_column_foreign_key_index(db):
    """Same leading column: ix_job_enquiries_job_recent also serves the foreign key (and the ON DELETE CASCADE of a
    posting). A fresh database never gets the single-column twin; an existing one loses it in
    create_indexes_concurrently(), once the replacement is valid - startup (sync_schema) never drops anything."""

    async def present() -> set[str]:
        async with dbmod.get_engine().connect() as conn:
            return set((await conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'job_enquiries'"))).scalars())

    assert "ix_job_enquiries_job_recent" in await present() and "ix_job_enquiries_job_id" not in await present()
    assert JobEnquiry.__table__.info[dbmod.SUPERSEDED_INDEXES] == {"ix_job_enquiries_job_id": "ix_job_enquiries_job_recent"}
    async with dbmod.get_engine().begin() as conn:  # a database created by the previous version
        await conn.execute(text("CREATE INDEX ix_job_enquiries_job_id ON job_enquiries (job_id)"))
        assert await dbmod.sync_schema(conn) == []
    assert "ix_job_enquiries_job_id" in await present()
    assert "drop ix_job_enquiries_job_id" in await dbmod.create_indexes_concurrently(dbmod.get_engine())
    assert "ix_job_enquiries_job_id" not in await present()
