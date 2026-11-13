"""One table of rules for every AI feature: quota group and cost, queue priority, how long a request may wait in
the AI queue, what happens to the work when its requester leaves, and the per-role quotas + cooldown ladder.

Read by app/ai/guard.py (spam protection), app/ai/scheduler.py (fair queue + cancellation) and
app/services/jobs.py (exit policies of background work). Change limits here, not at call sites.
See docs/ai-guard-and-queue.md for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass

# queue priorities (lower runs first; waiting background work is aged so it never starves)
REALTIME = 0  # someone is mid-workout and listening (live coach replies, coach voice)
INTERACTIVE = 1  # someone is looking at a spinner (assistant, Q&A, scans, intake)
BACKGROUND = 2  # nobody waits on screen (jobs: notes, explanations, verification)

# exit policies: what happens to queued/running work when the person who asked for it leaves
EXIT_CANCEL = "cancel"  # only useful while they wait on screen -> stop it, free the GPU
EXIT_PAUSE = "pause"  # result is stored for later -> defer it until they come back
EXIT_KEEP = "keep"  # an obligation (verification, payroll, safety, security) -> always finish

# queue lanes (independent capacity): LLM calls vs. Kokoro speech synthesis
LANE_LLM = "llm"
LANE_TTS = "tts"


@dataclass(frozen=True, slots=True)
class FeaturePolicy:
    name: str
    group: str  # quota group: chat | qa | gen | notes | vision | voice | system
    cost: int = 1  # quota units one call uses
    priority: int = INTERACTIVE
    max_wait: float = 25.0  # seconds an interactive request may wait for a queue slot before falling back
    on_exit: str = EXIT_CANCEL
    guarded: bool = True  # per-user quotas, duplicate/spam checks and cooldowns apply (False: system/admin work)
    safety: bool = False  # part of a safety decision: never refused by quota/cooldown/spam rules (still counted)
    dedupe_seconds: float = 30.0  # the same text from the same user within this window is refused (0 = off)
    lane: str = LANE_LLM


FEATURES: dict[str, FeaturePolicy] = {p.name: p for p in (
    # --- interactive, someone waits on screen: cancel when they leave
    FeaturePolicy("live_coach", "chat", priority=REALTIME, max_wait=8.0, dedupe_seconds=4.0),
    FeaturePolicy("tts", "voice", priority=REALTIME, max_wait=8.0, dedupe_seconds=0.0, lane=LANE_TTS),
    FeaturePolicy("assistant", "chat", max_wait=20.0),
    FeaturePolicy("plans", "qa", max_wait=12.0),
    FeaturePolicy("shop", "qa", max_wait=12.0),
    FeaturePolicy("intake", "gen", max_wait=25.0, dedupe_seconds=20.0),
    FeaturePolicy("health_scan", "vision", cost=2, max_wait=30.0, dedupe_seconds=0.0),
    FeaturePolicy("wellness_face", "vision", cost=2, max_wait=30.0, dedupe_seconds=0.0),
    FeaturePolicy("selftest", "system", max_wait=15.0, guarded=False, dedupe_seconds=0.0),
    # the super-admin's "Ask the AI" site overview (app/services/admin_insights.py), answered from the cached digest
    # only: the superadmin tier's 'qa' quotas, 2 units a question (the digest makes a long prompt)
    FeaturePolicy("admin_overview", "qa", cost=2, max_wait=30.0, dedupe_seconds=20.0),
    # --- user-owned enrichment stored for later: pause while they are away
    FeaturePolicy("coach", "gen", cost=3, priority=BACKGROUND, max_wait=120.0, on_exit=EXIT_PAUSE, dedupe_seconds=0.0),
    # the AC advice wording has a quota group of its own: editing conditions never uses up the coach plan quota
    FeaturePolicy("health_ac", "notes", priority=BACKGROUND, max_wait=120.0, on_exit=EXIT_PAUSE, dedupe_seconds=0.0),
    # the friendly AI note of a normal health reading (the health_ai_note job): judged when the reading is posted - a
    # note for later, never the 'health_metrics' safety review
    FeaturePolicy("health_note", "notes", priority=BACKGROUND, max_wait=120.0, on_exit=EXIT_PAUSE, dedupe_seconds=0.0),
    FeaturePolicy("gear", "gen", priority=BACKGROUND, max_wait=60.0, on_exit=EXIT_PAUSE, dedupe_seconds=0.0),
    # --- safety: an abnormal health reading may escalate to a suspension - never blocked, never dropped
    FeaturePolicy("health_metrics", "system", priority=INTERACTIVE, max_wait=45.0, on_exit=EXIT_KEEP, safety=True,
                  dedupe_seconds=0.0),
    # --- the OCR vision step: a person's request (e.g. /me/health/scan) is charged to their 'vision' quota; jobs and
    #     system work (identity, branch, payment and certificate verification) are never judged by the guard
    FeaturePolicy("ocr", "vision", priority=BACKGROUND, max_wait=300.0, on_exit=EXIT_KEEP, dedupe_seconds=0.0),
    # --- obligations / system work: never cancelled because someone left, not subject to per-user quotas
    FeaturePolicy("id_card", "system", priority=BACKGROUND, max_wait=300.0, on_exit=EXIT_KEEP, guarded=False,
                  dedupe_seconds=0.0),
    FeaturePolicy("branch_verification", "system", priority=BACKGROUND, max_wait=600.0, on_exit=EXIT_KEEP,
                  guarded=False, dedupe_seconds=0.0),
    FeaturePolicy("staff_certification", "system", priority=BACKGROUND, max_wait=600.0, on_exit=EXIT_KEEP,
                  guarded=False, dedupe_seconds=0.0),
    FeaturePolicy("payroll_explain", "system", priority=BACKGROUND, max_wait=600.0, on_exit=EXIT_KEEP, guarded=False,
                  dedupe_seconds=0.0),
    FeaturePolicy("security_triage", "system", priority=BACKGROUND, max_wait=300.0, on_exit=EXIT_KEEP, guarded=False,
                  dedupe_seconds=0.0),
)}

DEFAULT_POLICY = FeaturePolicy("default", "gen", dedupe_seconds=0.0)


def policy_for(feature: str | None) -> FeaturePolicy:
    return FEATURES.get(feature or "", DEFAULT_POLICY)


# --------------------------------------------------------------------------- per-user quotas
# (per minute, per hour, per day) in quota units, by account tier and quota group. Multiplied by
# settings.ai_quota_scale. Tiers: member_ai = customer whose active plan includes AI; member = customer without
# an AI plan (assistant commands and plan/product questions still work, just fewer); staff; owner; superadmin.
# Groups: chat (assistant, live coach), qa (plan / product questions), gen (intake, coach plans, gear), notes (the
# background AC-advice wording), vision (photo checks and the OCR vision step), voice (coach voice). A group a tier
# does not list is unlimited for it but still counted ('system').
Window = tuple[int, int, int]
QUOTAS: dict[str, dict[str, Window]] = {
    "member_ai": {"chat": (12, 150, 500), "qa": (8, 60, 200), "gen": (3, 12, 30), "notes": (6, 40, 120),
                  "vision": (4, 20, 40), "voice": (90, 1500, 5000)},
    "member": {"chat": (5, 30, 80), "qa": (6, 30, 100), "gen": (2, 6, 12), "notes": (3, 15, 40), "vision": (3, 10, 20),
               "voice": (60, 600, 2000)},
    "staff": {"chat": (10, 120, 400), "qa": (8, 60, 200), "gen": (6, 30, 100), "notes": (6, 40, 120),
              "vision": (4, 30, 80), "voice": (60, 600, 2000)},
    "owner": {"chat": (10, 120, 400), "qa": (8, 60, 200), "gen": (10, 60, 300), "notes": (6, 40, 120),
              "vision": (6, 60, 200), "voice": (60, 600, 2000)},
    "superadmin": {"chat": (30, 600, 3000), "qa": (30, 600, 3000), "gen": (30, 300, 1500), "notes": (30, 200, 600),
                   "vision": (20, 300, 1500), "voice": (120, 3000, 10000)},
}
WINDOW_SECONDS: Window = (60, 3600, 86400)

# concurrency per user (queued + running is settings.ai_queue_max_per_user)
MAX_RUNNING_PER_USER = 2

# --------------------------------------------------------------------------- strikes -> cooldowns -> bans
# Rejections earn strike points (rolling STRIKE_WINDOW). Reaching a level starts an AI cooldown for the account
# (all guarded AI features answer 429 "AI paused" until it ends; safety features are never blocked).
STRIKE_WINDOW = 15 * 60
STRIKE_POINTS = {"quota": 1, "too_many": 1, "duplicate": 1, "spam": 3, "flood": 2, "injection": 1, "abort": 1}
DUPLICATE_FREE = 2  # duplicates within the window that cost no points (double-clicks, impatience)
# 'abort': the requester dropped an AI call after it reached a provider (tab, sheet or connection closed mid-answer).
# Such a call is charged anyway; dropping answers again and again is an abuse signal, a few drops are just people.
ABORT_FREE = 5  # mid-answer drops within the window that cost no points
FLOOD_BURST = (5, 10.0)  # 5+ rejections within 10 s add a 'flood' strike
COOLDOWN_LADDER: tuple[tuple[int, int], ...] = ((6, 2 * 60), (12, 15 * 60), (20, 60 * 60))  # (points, seconds)
REPEAT_COOLDOWN_SECONDS = 24 * 3600  # the 3rd cooldown within a day lasts 24 h ...
REPEAT_COOLDOWN_AFTER = 3
# ... and cooldowns of 60 min or more are reported to the security system (category ai_abuse), whose usual
# policy then applies to the IP: subscribers 2-24 h bans, repeat offenders without a subscription banned.
ESCALATE_MIN_SECONDS = 60 * 60
ESCALATE_SEVERITY = {60 * 60: 2, REPEAT_COOLDOWN_SECONDS: 3}

# text limits for guarded features (routes validate their own, tighter, limits first)
MAX_TEXT_CHARS = 4000
