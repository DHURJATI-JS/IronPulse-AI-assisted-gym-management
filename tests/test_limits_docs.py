"""README "Limits" is the one list of every user-visible limit (approved load-safety limits): each row names the
constant / setting that holds the value, and these tests read that value from the code and require the row to state it
- a limit changed in the code without its row (or a row copied without checking) fails here. .env.example documents
the per-person caps with their real defaults, commented out (a new .env never pins them)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from app.config import BASE_DIR, Settings

README = BASE_DIR / "README.md"


def _rows() -> list[list[str]]:
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## Limits\n")
    end = text.index("\n## ", start + 10)
    rows = []
    for line in text[start:end].splitlines():
        if line.startswith("| **"):
            cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
            assert len(cells) == 3, line
            rows.append(cells)
    return rows


def _row(symbol: str, where: str = "") -> list[str]:
    """The row whose "where to change it" cell names `symbol` (and the file ``where``, when given)."""
    found = [r for r in _rows() if f"`{symbol}`" in r[2] and where in r[2]]
    assert found, f"no Limits row names `{symbol}` {where}"
    return found[0]


def default(name: str) -> Any:
    return Settings.model_fields[name].default


def const(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def hours(td: timedelta) -> str:
    return f"{td.total_seconds() / 3600:g} h"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# (symbol named in the row, file named with it, the value text that row must state - computed from the code)
SHOP, PUBLIC, FINANCE = "app/services/shop.py", "app/routers/public.py", "app/routers/owner_finance.py"
SA, POLICY = "app/routers/superadmin.py", "app/ai/policy.py"
FP, RL = "app/services/fitness_profile.py", "app/security/ratelimit.py"
STORAGE = "app/services/storage.py"


def _checks() -> list[tuple[str, str, Callable[[], str]]]:
    c, s = const, default
    storage = "app.services.storage"
    acc, health_r, tasks, chat_s = "app.routers.account", "app.routers.customer_health", "app.services.tasks", "app.services.chat"
    shop, pub, oc, ost = "app.services.shop", "app.routers.public", "app.routers.owner_customers", "app.routers.owner_staff"
    fin, sa, pol = "app.routers.owner_finance", "app.routers.superadmin", "app.ai.policy"
    fp, mem, rl = "app.services.fitness_profile", "app.ai.memory", "app.security.ratelimit"
    quotas = c(pol, "QUOTAS")["member_ai"]
    ladder = c(pol, "COOLDOWN_LADDER")
    return [
        # accounts and health
        ("PAGE_SIZE", "app/routers/account.py", lambda: f"{c(acc, 'PAGE_SIZE')} per window"),
        ("FEED_MAX", "app/routers/account.py", lambda: f"at most {c(acc, 'FEED_MAX')}"),
        ("COUNT_CAP", "app/routers/account.py", lambda: f"**{c(acc, 'COUNT_CAP'):,}+**"),
        ("MAX_CONDITIONS", "app/routers/customer_health.py", lambda: f"{c(health_r, 'MAX_CONDITIONS')} per member"),
        ("MAX_CONDITIONS", "app/services/health.py", lambda: f"{c('app.services.health', 'MAX_CONDITIONS')} per member"),
        ("MAX_ITEMS", "app/services/intake.py", lambda: f"{c('app.services.intake', 'MAX_ITEMS')} per list × 4 lists"),
        ("MAX_ITEMS", "app/services/intake.py",
         lambda: f"adds at most {c('app.services.intake', 'MAX_ITEMS')} entries to a list per save"),
        ("EDIT_TEXT_MAX", "app/services/intake.py",
         lambda: f"{c('app.services.intake', 'EDIT_TEXT_MAX'):,} characters per list"),
        ("SNAPSHOT_MAX_AGE", "app/services/intake.py",
         lambda: f"valid {c('app.services.intake', 'SNAPSHOT_MAX_AGE') // 3600} h"),
        ("SNAPSHOT_ROWS_MAX", "app/services/intake.py",
         lambda: f"names at most {c('app.services.intake', 'SNAPSHOT_ROWS_MAX')}"),
        ("ALERTS_PER_DAY", "app/services/health.py",
         lambda: f"{c('app.services.health', 'ALERTS_PER_DAY')} new health alerts per member per day"),
        ("REVIEWS_PER_DAY", "app/services/health.py",
         lambda: f"{c('app.services.health', 'REVIEWS_PER_DAY')} AI safety reviews"),
        ("INJECTION_SCAN_MAX", "app/ai/member_context.py",
         lambda: f"at most {c('app.ai.member_context', 'INJECTION_SCAN_MAX'):,} characters read"),
        ("CONDITIONS_READ_MAX", "app/services/health.py",
         lambda: f"{c('app.services.health', 'CONDITIONS_READ_MAX')}, safety-relevant first"),
        ("HISTORY_PER_PAGE", "", lambda: f"{c(health_r, 'HISTORY_PER_PAGE')} readings per page"),
        ("ALERTS_PER_PAGE", "", lambda: f"alerts {c(health_r, 'ALERTS_PER_PAGE')} per page"),
        ("ALERTS_SHOWN", "", lambda: f"the latest {c(health_r, 'ALERTS_SHOWN')}"),
        # what the AI remembers, memory offers, per-day caps of the AI features (AI-memory builder)
        ("LIST_CAPS", FP, lambda: "{likes} exercise types enjoyed and {avoids} avoided, {goals} goals, {equipment} "
                                  "equipment items".format(**c(fp, "LIST_CAPS"))),
        ("ITEM_MAX", FP, lambda: "own labels {likes} characters (goals {goals})".format(**c(fp, "ITEM_MAX"))),
        ("NOTES_MAX", FP, lambda: f"notes {c(fp, 'NOTES_MAX')} characters"),
        ("SESSIONS_RANGE", FP, lambda: "{}-{} sessions a week".format(*c(fp, "SESSIONS_RANGE"))),
        ("MINUTES_RANGE", FP, lambda: "{}-{} minutes".format(*c(fp, "MINUTES_RANGE"))),
        ("FORGOTTEN_MAX", FP, lambda: f"{c(fp, 'FORGOTTEN_MAX')} forget requests remembered"),
        ("MAX_PROPOSALS", "app/ai/memory.py", lambda: f'{c(mem, "MAX_PROPOSALS")} "Remember this?" offers per message'),
        ("FORGET_MAX", "app/ai/memory.py", lambda: f"{c(mem, 'FORGET_MAX')} items per forget"),
        ("TOKEN_MAX_AGE", "app/ai/memory.py", lambda: f"tickets valid {c(mem, 'TOKEN_MAX_AGE') // 3600} h"),
        ("ASSISTANT_HISTORY_MAX", "app/ai/assistant.py",
         lambda: f"the newest {c('app.ai.assistant', 'ASSISTANT_HISTORY_MAX'):,} messages per person"),
        ("SESSIONS_PER_DAY", "app/services/live_coach.py",
         lambda: f"{c('app.services.live_coach', 'SESSIONS_PER_DAY')} started per 24 h"),
        ("SESSION_EVENTS_MAX", "app/services/live_coach.py",
         lambda: f"{c('app.services.live_coach', 'SESSION_EVENTS_MAX'):,} events logged per workout"),
        ("PLANS_PER_DAY", "app/services/coach.py", lambda: f"{c('app.services.coach', 'PLANS_PER_DAY')} generated per 24 h"),
        # tasks and chat
        ("TASKS_MAX_OPEN", "", lambda: f"{s('tasks_max_open'):,} - the form"),
        ("MAX_OPEN_TASKS", "app/services/tasks.py", lambda: f"{c(tasks, 'MAX_OPEN_TASKS'):,} - the form"),
        ("CALENDAR_DAY_MAX", "", lambda: f"{c(tasks, 'CALENDAR_DAY_MAX')} tasks in the month grid"),
        ("TASKS_PER_PAGE", "", lambda: f"{c(tasks, 'TASKS_PER_PAGE')} per page"),
        ("TASKS_MAX_PER_PAGE", "", lambda: f"at most {c(tasks, 'TASKS_MAX_PER_PAGE')}"),
        ("RANGE_TASKS_MAX", "", lambda: f"at most {c(tasks, 'RANGE_TASKS_MAX'):,}"),
        ("THREAD_PAGE_SIZE", "", lambda: f"{c(chat_s, 'THREAD_PAGE_SIZE')} per window"),
        ("PAGE_SIZE", "app/services/chat.py", lambda: f"{c(chat_s, 'PAGE_SIZE')} per window"),
        ("MAX_BODY", "app/services/chat.py", lambda: f"{c(chat_s, 'MAX_BODY'):,} characters"),
        ("REPORTS_PER_PAGE", "", lambda: f"{c('app.routers.chat', 'REPORTS_PER_PAGE')} per page"),
        ("OWNER_REPORT_PAGE", "", lambda: f"{c('app.routers.chat', 'OWNER_REPORT_PAGE')} per page"),
        ("chat_messages_per_staff_day", "app/services/chat.py", lambda: "{:,} chat messages per staff member".format(
            next(cap.default for cap in c(chat_s, "CHAT_CAPS") if cap.name == "chat_messages_per_staff_day"))),
        ("chat_staff_messages_per_conversation_day", "app/services/chat.py",
         lambda: "{:,} gym-team messages per conversation".format(next(
             cap.default for cap in c(chat_s, "CHAT_CAPS") if cap.name == "chat_staff_messages_per_conversation_day"))),
        ("REPORT_CAPS", "app/routers/chat.py", lambda: "{} reports waiting for a reply and {} new reports".format(
            *(cap.default for cap in c("app.routers.chat", "REPORT_CAPS")))),
        # shop
        ("MAX_CART_LINES", SHOP, lambda: f"{c(shop, 'MAX_CART_LINES')} different products"),
        ("MAX_STORE_LINES", SHOP, lambda: f"{c(shop, 'MAX_STORE_LINES')} of them from one store"),
        ("WISHLIST_MAX", SHOP, lambda: f"{c(shop, 'WISHLIST_MAX')} products"),
        ("WISHLIST_PAGE_SIZE", SHOP, lambda: f"{c(shop, 'WISHLIST_PAGE_SIZE')} per page"),
        ("PAGE_SIZE", SHOP, lambda: f"{c(shop, 'PAGE_SIZE')} per page"),
        ("CATALOGUE_MAX_PAGE", SHOP, lambda: f"at most {c(shop, 'CATALOGUE_MAX_PAGE')} pages"),
        ("CATALOGUE_MAX_PAGE", SHOP, lambda: f"**{c(shop, 'PAGE_SIZE') * c(shop, 'CATALOGUE_MAX_PAGE'):,}+**"),
        ("STORE_OPTIONS", SHOP, lambda: f"the {c(shop, 'STORE_OPTIONS')} stores"),
        ("STORE_SEARCH_LIMIT", SHOP, lambda: f"store search of {c(shop, 'STORE_SEARCH_LIMIT')} results"),
        ("FACETS_TTL", SHOP, lambda: f"cached {c(shop, 'FACETS_TTL'):g} s"),
        ("REVIEWS_PAGE_SIZE", SHOP, lambda: f"{c(shop, 'REVIEWS_PAGE_SIZE')} at a time"),
        ("ORDERS_PAGE_SIZE", SHOP, lambda: f"{c(shop, 'ORDERS_PAGE_SIZE')} per page"),
        ("ORDER_EVENTS_LIMIT", "app/routers/shop.py",
         lambda: f"latest {c('app.routers.shop', 'ORDER_EVENTS_LIMIT')} events"),
        ("MAX_CONDITIONS", SHOP, lambda: f"reads {c(shop, 'MAX_CONDITIONS')} of"),
        ("PROMPT_CONDITIONS", SHOP, lambda: f"at most {c(shop, 'PROMPT_CONDITIONS')}"),
        ("ITEM_PROMPT_MAX", "app/ai/shop.py", lambda: f"({c('app.ai.shop', 'ITEM_PROMPT_MAX')} characters each)"),
        ("MAX_ALLERGY_MENTIONS", SHOP, lambda: f'at most {c(shop, "MAX_ALLERGY_MENTIONS")} "Check the ingredients"'),
        ("STOCK_HOLD_MINUTES", SHOP, lambda: f"holds its units for {c(shop, 'STOCK_HOLD_MINUTES')} minutes"),
        ("RATINGS_EVERY_S", SHOP, lambda: "hourly" if c(shop, "RATINGS_EVERY_S") == 3600 else "every"),
        ("RATINGS_BATCH", SHOP, lambda: f"{c(shop, 'RATINGS_BATCH')} products per transaction"),
        ("RATINGS_BUDGET_S", SHOP, lambda: f"{c(shop, 'RATINGS_BUDGET_S'):g} s per run"),
        ("LOW_STOCK_REPEAT_HOURS", SHOP, lambda: f"in {c(shop, 'LOW_STOCK_REPEAT_HOURS')} h"),
        ("LOW_STOCK_LISTED", SHOP, lambda: f"naming {c(shop, 'LOW_STOCK_LISTED')} products"),
        # public pages and job enquiries
        ("HOME_TTL", PUBLIC, lambda: f"cached {c(pub, 'HOME_TTL'):g} s"),
        ("STATS_TTL", PUBLIC, lambda: f"figures {c(pub, 'STATS_TTL'):g} s"),
        ("NEAR_SORT_KM", PUBLIC, lambda: f"{c(pub, 'PAGE_SIZE')} per page"),
        ("NEAR_SORT_KM", PUBLIC, lambda: f"within {c(pub, 'NEAR_SORT_KM'):g} km"),
        ("DIRECTORY_CITIES", PUBLIC, lambda: f"the {c(pub, 'DIRECTORY_CITIES')} busiest cities"),
        ("CITIES_TTL", PUBLIC, lambda: f"cached {c(pub, 'CITIES_TTL'):g} s"),
        ("BRANCH_PAGE_SIZE", PUBLIC, lambda: f"{c(pub, 'BRANCH_PAGE_SIZE')} branch cards"),
        ("BRANCH_PAGE_SIZE", PUBLIC, lambda: f"{c(pub, 'BRANCH_PAGE_SIZE')} per page"),
        ("JOBS_PAGE_SIZE", PUBLIC, lambda: f"{c(pub, 'JOBS_PAGE_SIZE')} per page"),
        ("JOB_RADII_KM", PUBLIC, lambda: "radius " + " / ".join(
            f"{r:g}" for r in (*c(pub, "JOB_RADII_KM"), s("offline_max_distance_km"))) + " km"),
        ("OFFLINE_MAX_DISTANCE_KM", "", lambda: f"up to {s('offline_max_distance_km'):g} km"),
        ("ENQUIRY_MAX_CHARS", PUBLIC,
         lambda: f"{c(pub, 'ENQUIRY_MIN_CHARS')}-{c(pub, 'ENQUIRY_MAX_CHARS'):,} characters"),
        ("ENQUIRY_MAX_LINKS", PUBLIC, lambda: f"at most {c(pub, 'ENQUIRY_MAX_LINKS')} links"),
        ("ENQUIRY_MIN_FILL_SECONDS", PUBLIC, lambda: f"at least {c(pub, 'ENQUIRY_MIN_FILL_SECONDS')} s"),
        ("ENQUIRY_FORM_MAX_AGE", PUBLIC, lambda: f"at most {c(pub, 'ENQUIRY_FORM_MAX_AGE') / 86400:g} days"),
        ("ENQUIRY_COOLDOWN_SECONDS", PUBLIC, lambda: f"per {c(pub, 'ENQUIRY_COOLDOWN_SECONDS') / 60:g} min"),
        ("ENQUIRY_DUPLICATE_HOURS", PUBLIC, lambda: f"for {c(pub, 'ENQUIRY_DUPLICATE_HOURS')} h"),
        # owners: members, audit, staff
        ("HEALTH_LABELS_SHOWN", "", lambda: f"{c(oc, 'PAGE_SIZE')} per page"),
        ("HEALTH_LABELS_SHOWN", "", lambda: f"{c(oc, 'HEALTH_LABELS_SHOWN')} health chips"),
        ("SUBS_PAGE_SIZE", "", lambda: f"memberships {c(oc, 'SUBS_PAGE_SIZE')} per page"),
        ("CONDITIONS_PER_KIND", "app/routers/owner_customers.py", lambda: f"{c(oc, 'CONDITIONS_PER_KIND')} per kind"),
        ("CONDITIONS_SHOWN", "app/routers/owner_customers.py", lambda: f"{c(oc, 'CONDITIONS_SHOWN')} in all"),
        ("AUDIT_PAGE_SIZE", "", lambda: f"{c(oc, 'AUDIT_PAGE_SIZE')} per page"),
        ("FORMER_MEMBER_DAYS", "app/services/intake.py",
         lambda: f"expired within the last {c('app.services.intake', 'FORMER_MEMBER_DAYS')} days"),
        ("INVITE_RESEND_INTERVAL", "app/routers/owner_customers.py",
         lambda: f"per member per {c(oc, 'INVITE_RESEND_INTERVAL').total_seconds() / 60:g} min"),
        ("INVITES_PER_MEMBER_DAY", "app/routers/owner_customers.py",
         lambda: f"at most {c(oc, 'INVITES_PER_MEMBER_DAY')} in any 24 h"),
        ("STAFF_INVITE_RESENDS_PER_DAY", "app/routers/owner_staff.py",
         lambda: f"at most {c(ost, 'STAFF_INVITE_RESENDS_PER_DAY')} in any 24 h"),
        ("INVITE_RESEND_SECONDS", "app/routers/owner_staff.py", lambda: f"per {c(ost, 'INVITE_RESEND_SECONDS')} s"),
        ("WITHDRAW_PENDING_MAX", "app/services/trust_safety.py",
         lambda: f"at most {c('app.services.trust_safety', 'WITHDRAW_PENDING_MAX')} of the person's other pending"),
        ("AUDIT_COUNT_CAP", "", lambda: f"**{c(oc, 'AUDIT_COUNT_CAP'):,}+**"),
        ("STAFF_PER_PAGE", "", lambda: f"{c(ost, 'STAFF_PER_PAGE')} per page"),
        ("STAFF_PER_PAGE", "", lambda: f"{c(ost, 'CLIENTS_PER_PAGE')} per page"),
        ("TIMELINE_LIMIT", "", lambda: f"{c(ost, 'TIMELINE_LIMIT')} events per window"),
        ("JOBS_PER_PAGE", "", lambda: f"{c(ost, 'JOBS_PER_PAGE')} and {c(ost, 'ENQUIRIES_PER_PAGE')} per page"),
        ("ELIGIBLE_LIMIT", "", lambda: f"{c('app.services.staffing', 'ELIGIBLE_LIMIT')} members at once"),
        ("TRAINER_MAX_CLIENTS", "", lambda: f"{s('trainer_max_clients')} active clients per trainer"),
        ("TRAINER_NOTICE_HOURS", "", lambda: f"per {c('app.services.checkout', 'TRAINER_NOTICE_HOURS')} h"),
        ("REREAD_COOLDOWN_SECONDS", "",
         lambda: f"once per {c('app.services.ocr', 'REREAD_COOLDOWN_SECONDS') / 60:g} min"),
        ("REREAD_LIMIT", "", lambda: f"at most {c('app.services.ocr', 'REREAD_LIMIT')} times"),
        ("REREAD_BATCH", "", lambda: f"{c('app.services.ocr', 'REREAD_BATCH')} certificates per request"),
        ("CONDITIONS_SHOWN", "app/routers/staff_portal.py",
         lambda: f"{c('app.routers.staff_portal', 'CLIENTS_PER_PAGE')} clients per page"),
        ("CONDITIONS_PER_KIND", "app/routers/staff_portal.py",
         lambda: f"{c('app.routers.staff_portal', 'CONDITIONS_PER_KIND')} per kind"),
        ("CONDITIONS_SHOWN", "app/routers/staff_portal.py",
         lambda: f"{c('app.routers.staff_portal', 'CONDITIONS_SHOWN')} in all"),
        # owners: finance, payroll, shop
        ("DEFAULT_PERIOD", FINANCE, lambda: f"the {c(fin, 'PERIODS')[c(fin, 'DEFAULT_PERIOD')][0].lower()} by default"),
        ("PERIODS", FINANCE, lambda: ", ".join(label.removeprefix("Last ") for label, _ in c(fin, "PERIODS").values())),
        ("PERIODS", FINANCE, lambda: f"{c(fin, 'PAGE_SIZE')} per page"),
        ("TOTALS_ROW_CAP", FINANCE, lambda: f"at most {c(fin, 'TOTALS_ROW_CAP'):,} transactions"),
        ("TOTALS_ROW_CAP", FINANCE, lambda: f'"{c(fin, "TOTALS_ROW_CAP"):,}+"'),
        ("MAX_TX_PAGE", FINANCE, lambda: f"at most {c(fin, 'MAX_TX_PAGE'):,}"),
        ("EXPORT_BATCH", FINANCE, lambda: f"batches of {c(fin, 'EXPORT_BATCH'):,} rows"),
        ("EXPORT_MAX_ROWS", FINANCE, lambda: f"above {c(fin, 'EXPORT_MAX_ROWS'):,} rows"),
        ("RECENT_TX_LIMIT", FINANCE, lambda: f"latest {c(fin, 'RECENT_TX_LIMIT')} transactions"),
        ("PENDING_STATEMENT_LIMIT", FINANCE, lambda: f"{c(fin, 'PENDING_STATEMENT_LIMIT')} unpaid statements"),
        ("STATEMENT_PAGE_SIZE", FINANCE, lambda: f"statements {c(fin, 'STATEMENT_PAGE_SIZE')} per page"),
        ("EMPLOYEE_OPTIONS_LIMIT", FINANCE, lambda: f"offers {c(fin, 'EMPLOYEE_OPTIONS_LIMIT')} people"),
        ("GENERATE_ATTEMPTS", "", lambda: f"up to {c('app.services.payroll', 'GENERATE_ATTEMPTS')} attempts"),
        ("EXPLAIN_PER_RUN", "", lambda: f"at most {c('app.services.payroll', 'EXPLAIN_PER_RUN')} AI explanations"),
        ("ORDERS_PER_PAGE", "app/routers/owner_shop.py", lambda: "orders {}, products {}, coupons {}, reviews {} per page".format(
            *(c("app.routers.owner_shop", n) for n in ("ORDERS_PER_PAGE", "PRODUCTS_PER_PAGE", "COUPONS_PER_PAGE",
                                                        "REVIEWS_PER_PAGE")))),
        ("ORDER_EVENTS_LIMIT", "app/routers/owner_shop.py",
         lambda: f"latest {c('app.routers.owner_shop', 'ORDER_EVENTS_LIMIT')} events"),
        # super-admin
        ("DEFAULT_EVENT_WINDOW", SA, lambda: f"the {c(sa, 'EVENT_WINDOW_MAP')[c(sa, 'DEFAULT_EVENT_WINDOW')][0].lower()} by default"),
        ("EVENTS_PER_PAGE", SA, lambda: f"{c(sa, 'EVENTS_PER_PAGE')} events per page"),
        ("COUNT_CAP", SA, lambda: f"**{c(sa, 'COUNT_CAP'):,}+**"),
        ("SCAN_CAP", SA, lambda: f"newest {c(sa, 'SCAN_CAP'):,} matching events"),
        ("USERS_PER_PAGE", SA, lambda: "users {}, bans {}, outbox {}, audit {} per page".format(
            *(c(sa, n) for n in ("USERS_PER_PAGE", "BANS_PER_PAGE", "OUTBOX_PER_PAGE", "AUDIT_PER_PAGE")))),
        ("QUEUE_LIMIT", SA, lambda: f"review queues {c(sa, 'QUEUE_LIMIT')} at a time"),
        ("AUDIT_MAX_ACTIONS", SA, lambda: f"reads {c(sa, 'AUDIT_MAX_ACTIONS')} distinct actions"),
        ("AI_WINDOW", SA, lambda: f"top {c(sa, 'TOP_AI_USERS')} users over the last {hours(c(sa, 'AI_WINDOW'))}"),
        ("AI_BLOCKS_WINDOW", SA, lambda: f"over {c(sa, 'AI_BLOCKS_WINDOW').days} days ({c(sa, 'AI_BLOCKS_SHOWN')} shown)"),
        ("AI_DEMO_COUNT_TTL", SA, lambda: f"cached {c(sa, 'AI_DEMO_COUNT_TTL'):g} s"),
        ("STALE_AFTER", "", lambda: f"older than {hours(c('app.services.security_triage', 'STALE_AFTER'))}"),
        ("PENDING_COUNT_CAP", "", lambda: f"at {c('app.services.security_triage', 'PENDING_COUNT_CAP'):,}"),
        # AI
        ("QUOTAS", POLICY, lambda: "chat {} / {} / {:,}".format(*quotas["chat"])),
        ("QUOTAS", POLICY, lambda: "plan and product questions {} / {} / {:,}".format(*quotas["qa"])),
        ("QUOTAS", POLICY, lambda: "plans and intake {} / {} / {:,}".format(*quotas["gen"])),
        ("QUOTAS", POLICY, lambda: "photo checks {} / {} / {:,}".format(*quotas["vision"])),
        ("QUOTAS", POLICY, lambda: "coach voice {} / {:,} / {:,}".format(*quotas["voice"])),
        ("STRIKE_WINDOW", POLICY, lambda: f"over {c(pol, 'STRIKE_WINDOW') / 60:g} min"),
        ("STRIKE_POINTS", POLICY, lambda: "spam {spam}, flood {flood}".format(**c(pol, "STRIKE_POINTS"))),
        ("FLOOD_BURST", POLICY, lambda: "{} refusals within {:g} s".format(*c(pol, "FLOOD_BURST"))),
        ("DUPLICATE_FREE", POLICY, lambda: f"the first {c(pol, 'DUPLICATE_FREE')} duplicates"),
        ("ABORT_FREE", POLICY, lambda: f"the first {c(pol, 'ABORT_FREE')} abandoned answers"),
        ("COOLDOWN_LADDER", POLICY, lambda: "{} points pause AI for {:g} min, {} for {:g} min, {} for {:g} min".format(
            *(x for points, seconds in ladder for x in (points, seconds / 60)))),
        ("REPEAT_COOLDOWN_SECONDS", POLICY, lambda: "the {} pause in a day lasts {:g} h".format(
            ordinal(c(pol, "REPEAT_COOLDOWN_AFTER")), c(pol, "REPEAT_COOLDOWN_SECONDS") / 3600)),
        ("MAX_RUNNING_PER_USER", POLICY, lambda: f"{s('ai_queue_max_per_user')} queued + running calls per person "
                                                 f"({c(pol, 'MAX_RUNNING_PER_USER')} running)"),
        ("AI_QUEUE_MAX_TOTAL", "", lambda: f"{s('ai_queue_max_total')} in the whole queue"),
        ("AI_QUEUE_CONCURRENCY", "", lambda: f"{s('ai_queue_concurrency')} language-model calls"),
        ("AI_TTS_CONCURRENCY", "", lambda: f"{s('ai_tts_concurrency')} voice syntheses"),
        ("MAX_TEXT_CHARS", POLICY, lambda: f"{c(pol, 'MAX_TEXT_CHARS'):,} characters per guarded request"),
        ("MAX_TEXT", "app/services/tts.py", lambda: f"{c('app.services.tts', 'MAX_TEXT')} characters per voice line"),
        ("PRESENCE_GRACE_SECONDS", "", lambda: f"{s('presence_grace_seconds')} s later"),
        ("AI_DOCKER_FIRST_TOKEN_SECONDS", "",
         lambda: f"at least {s('ai_docker_first_token_seconds'):g} s for its first words"),
        # network
        ("RATE_LIMIT_PER_MINUTE", "", lambda: f"{s('rate_limit_per_minute')} requests/min"),
        ("RATE_LIMIT_AUTH_PER_MINUTE", "", lambda: f"sign-in {s('rate_limit_auth_per_minute')}/min"),
        ("RATE_LIMIT_AI_PER_MINUTE", "", lambda: f"AI {s('rate_limit_ai_per_minute')}/min"),
        ("RATE_LIMIT_LIVE_PER_MINUTE", "", lambda: f"live workout {s('rate_limit_live_per_minute')}/min"),
        ("RATE_LIMIT_UPLOAD_PER_MINUTE", "", lambda: f"uploads {s('rate_limit_upload_per_minute')}/min"),
        ("RATE_LIMIT_STATIC_PER_MINUTE", "", lambda: f"static files {s('rate_limit_static_per_minute'):,}/min"),
        ("WS_MAX_SOCKETS_PER_USER", "", lambda: f"{c('app.routers.core', 'WS_MAX_SOCKETS_PER_USER')} open sockets"),
        ("WS_FLOOD_MESSAGES", "", lambda: "{} messages per {:g} s".format(
            c("app.routers.core", "WS_FLOOD_MESSAGES"), c("app.routers.core", "WS_FLOOD_WINDOW"))),
        ("WS_MAX_MESSAGE_CHARS", "", lambda: f"{c('app.routers.core', 'WS_MAX_MESSAGE_CHARS'):,} characters per message"),
        ("IPV6_PREFIX", RL, lambda: f"by its /{c(rl, 'IPV6_PREFIX')}"),
        ("MAX_CONNECTIONS", RL, lambda: f"at most {c(rl, 'MAX_CONNECTIONS')} Redis calls in flight per worker"),
        ("QUEUE_WAIT_SECONDS", RL, lambda: f"at most {c(rl, 'QUEUE_WAIT_SECONDS'):g} s for a slot"),
        ("RATE_LIMIT_REDIS_TIMEOUT_MS", "", lambda: f"slower than {s('rate_limit_redis_timeout_ms')} ms"),
        ("RETRY_SECONDS", RL, lambda: f"after {c(rl, 'RETRY_SECONDS'):g} s (±{c(rl, 'RETRY_JITTER') * 100:g} %"),
        ("IMMUTABLE", "app/core/static.py", lambda: "cached for 1 year" if c("app.core.static", "IMMUTABLE")
         == "public, max-age=31536000, immutable" else "the IMMUTABLE header changed"),
        # uploads
        ("MAX_UPLOAD_MB", "", lambda: f"a file at most {s('max_upload_mb')} MB"),
        ("MAX_IMAGE_PIXELS", STORAGE,
         lambda: f"an image at most {c(storage, 'MAX_IMAGE_PIXELS') / 1e6:g} megapixels as decoded"),
        ("MAX_JPEG_PIXELS", STORAGE, lambda: f"JPEG photo up to {c(storage, 'MAX_JPEG_PIXELS') / 1e6:g} megapixels"),
        ("IMAGE_DECODE_SLOTS", STORAGE,
         lambda: f"{c(storage, 'IMAGE_DECODE_SLOTS')} image decodes at once per worker process"),
        ("IMAGE_DECODE_WAIT_SECONDS", STORAGE, lambda: f"waits at most {c(storage, 'IMAGE_DECODE_WAIT_SECONDS')} s"),
        ("PDF_MAX_PAGES", STORAGE, lambda: f"a PDF 1-{c(storage, 'PDF_MAX_PAGES')} pages"),
        # background work
        ("CLAIM_LEASE", "app/services/mailer.py",
         lambda: f"{c('app.services.mailer', 'CLAIM_LEASE').total_seconds() / 60:g} min lease"),
        ("FLUSH_EVENTS_MAX", "app/security/bans.py",
         lambda: f"at most {c('app.security.bans', 'FLUSH_EVENTS_MAX'):,} security events"),
        ("MAX_BANS_PER_FLUSH", "app/security/bans.py",
         lambda: f"{c('app.security.bans', 'MAX_BANS_PER_FLUSH')} ban decisions"),
        ("CALL_LOG_FLUSH_MAX", "app/ai/client.py", lambda: f"{c('app.ai.client', 'CALL_LOG_FLUSH_MAX')} AI call logs"),
        ("MAX_ID", "", lambda: "outside 1 … 2⁶³-1" if c("app.routers.owner_customers", "MAX_ID") == 2**63 - 1
         else "MAX_ID changed"),
        # database
        ("DB_STATEMENT_TIMEOUT_MS", "", lambda: f"after {s('db_statement_timeout_ms') / 1000:g} s"),
        ("DB_IDLE_TX_TIMEOUT_MS", "", lambda: f"{s('db_idle_tx_timeout_ms') / 1000:g} s in a request"),
        ("DB_BACKGROUND_IDLE_TX_TIMEOUT_MS", "", lambda: f"{s('db_background_idle_tx_timeout_ms') / 60000:g} min for background"),
        ("DB_POOL_SIZE", "", lambda: f"{s('db_pool_size')} + {s('db_max_overflow')} overflow"),
        ("DB_POOL_TIMEOUT_S", "", lambda: f"up to {s('db_pool_timeout_s'):g} s"),
        ("BUDGET_SHARE", "app/db.py", lambda: f"more than {c('app.db', 'BUDGET_SHARE') * 100:g} %"),
        # retention
        *((f"RETENTION_{key}_DAYS", "", (lambda key=key: f"{s(f'retention_{key.lower()}_days')} days"))
          for key in ("SECURITY", "AUDIT", "AI_LOG", "EMAIL", "JOBS", "NOTIFICATION", "IP_BAN", "AUTH_TOKEN", "SESSION")),
    ]


@pytest.mark.parametrize("symbol, where, expected", _checks(), ids=lambda v: v if isinstance(v, str) else "")
def test_the_limits_table_states_the_value_in_the_code(symbol: str, where: str, expected: Callable[[], str]) -> None:
    row = _row(symbol, where)
    assert expected() in row[1], f"README Limits row for `{symbol}` should say {expected()!r}: {row[1]!r}"


def test_the_otp_bucket_row_matches_the_limiter() -> None:
    from app.security.ratelimit import limiter

    otp = limiter.limits()["otp"]
    minutes = otp.capacity / otp.per_second / 60
    row = _row("RATE_LIMIT_PER_MINUTE")
    assert f"OTP {otp.capacity:g} per {minutes:g} min" in row[1], row[1]


def test_every_retention_setting_and_section_is_listed() -> None:
    """Every RETENTION_*_DAYS setting has a row, and the section sits between Architecture and Quick start (and in
    the contents)."""
    names = {n for n in Settings.model_fields if n.startswith("retention_") and n.endswith("_days")}
    assert names and all(any(f"`{n.upper()}`" in r[2] for r in _rows()) for n in names), names
    text = README.read_text(encoding="utf-8")
    assert text.index("\n## Architecture\n") < text.index("\n## Limits\n") < text.index("\n## Quick start\n")
    assert "[Limits](#limits)" in text


def test_env_example_documents_the_per_person_caps_without_pinning_them() -> None:
    from dotenv import dotenv_values

    path = BASE_DIR / ".env.example"
    pinned = dotenv_values(path)
    text = path.read_text(encoding="utf-8")
    for key in ("TASKS_MAX_OPEN", "TRAINER_MAX_CLIENTS"):
        assert key not in pinned, f"{key} pinned in .env.example would override the default in every new .env"
        assert f"# {key}={default(key.lower())}" in text, key
