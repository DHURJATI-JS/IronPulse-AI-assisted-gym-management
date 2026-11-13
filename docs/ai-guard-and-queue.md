# AI spam protection and the stop-when-you-leave AI queue

This document is the contract between the modules that implement:

1. **AI spam protection** (`app/ai/guard.py`): per-user quotas, duplicate/flood/spam detection, escalating
   cooldowns that feed the existing ban system.
2. **A fair AI queue** (`app/ai/scheduler.py`): bounded concurrency, per-user fairness and priorities, queue
   waits that never count as provider failures, live queue positions.
3. **Halting work when the user leaves** (`app/services/presence.py`, `app/services/jobs.py` and the client
   `IP.ai`): closing a tab, navigating away, losing the connection or logging out stops the AI work nobody will
   see, and defers user-owned background work until the user comes back.

Foundation already in place: `app/ai/errors.py`, `app/ai/owner.py`, `app/ai/policy.py`, `app/core/state.py`,
the new `Job`/`AICallLog` columns, `JobStatus.CANCELLED|PAUSED`, `ThreatCategory.AI_ABUSE`, and the settings in
`app/config.py` (`ai_guard_enabled`, `ai_quota_scale`, `ai_queue_concurrency`, `ai_tts_concurrency`,
`ai_queue_max_per_user`, `ai_queue_max_total`, `presence_grace_seconds`, `jobs_concurrency`).

---

## 1. Who is spending compute: `AIOwner` (app/ai/owner.py)

`load_user` sets an `AIOwner` ContextVar for every signed-in HTTP request: `user_id`, `role`, `session_hash`,
`ip`, `page_id` (from the `X-Page-Id` header) and `disconnected` (`Request.is_disconnected`). The job runner sets
one per job (`kind="job"`, `user_id` = the job's user or None, `exit_policy`, `job_id`). Periodic/system work
has no owner. `OwnerContextMiddleware` clears it around every request. Plain HTML forms that start AI work post a
hidden `page_id` field; the route copies it with `owner.page_id = clean_page_id(form.get("page_id"))`.

## 2. Feature policies (app/ai/policy.py)

`policy_for(feature)` gives quota group/cost, priority (REALTIME < INTERACTIVE < BACKGROUND), `max_wait`,
`on_exit` (cancel | pause | keep), `guarded`, `safety`, `dedupe_seconds` and lane (llm | tts). Quotas per tier
(member_ai, member, staff, owner, superadmin) and the strike/cooldown ladder are there too. **Never hard-code
limits elsewhere.**

## 3. The guard (app/ai/guard.py)

```python
guard: AIGuard   # module singleton

async def admit(self, owner: AIOwner | None, feature: str, text: str | None = None) -> Admission | None
    # None (nothing to do) when: guard disabled, owner is None or not a user request (jobs/system), or the
    # feature is unguarded. Raises AIRejected(code, message, retry_after=...) otherwise. Order:
    #   1. active cooldown              -> 'cooldown' (safety features skip 1-4)
    #   2. text checks (if text)        -> 'spam' (repeated chars/tokens, link floods, mostly symbols, > MAX_TEXT_CHARS)
    #   3. duplicate of this user's own recent text for this feature (normalised sha256, dedupe_seconds window)
    #      -> 'duplicate' (first DUPLICATE_FREE duplicates cost no strike)
    #   4. quota windows (minute/hour/day, units = policy.cost, tier from role + AI entitlement) -> 'quota'
    #   Every rejection adds strike points; 5 rejections in 10 s add a 'flood' strike; strike totals start
    #   cooldowns along COOLDOWN_LADDER; cooldowns >= 60 min are reported to the security system.
async def settle(self, admission: Admission | None, *, ok: bool) -> None
    # ok=False (the work never reached a provider - refused or busy in the queue, cancelled while queued or waiting
    # for a provider slot, a stream stopped before its first words - or every provider failed, a stream cut by a
    # provider failure included): refund the units and forget the fingerprint. A call its requester stopped after a
    # provider worked on it is charged (ok=True) and its fingerprint forgotten (forget_fingerprint), so it may be
    # asked again at once. Idempotent.
async def forget_fingerprint(self, admission: Admission | None) -> bool  # charge stays, the same words may come again
async def note_abort(self, owner, feature) -> dict | None   # a call dropped mid-answer: strike beyond ABORT_FREE
async def status(self, user_id: int) -> dict   # {'cooldown_until', 'strikes', 'usage': {group: [m,h,d]}}
async def active_cooldowns(self, limit: int = 50) -> list[dict]   # admin panel
async def lift(self, user_id: int) -> bool                       # admin: clear cooldown + strikes
async def note_injection(self, owner, feature) -> None           # prompt-injection markers: strike, never block
def clean_text(value) -> str | None   # module function: lone surrogates -> U+FFFD (the router applies it once)
```

* **Entitlement:** a customer whose active subscription's plan `includes_ai` gets the `member_ai` tier,
  others get `member`. It is cached per user for 5 minutes and looked up in its own short DB session.
* **Storage:** Redis when `get_redis()` returns a client (shared by every process and both app copies), with keys
  namespaced `ip:<env>:<db-name>:ai:...`; otherwise an in-process store with the same semantics. On any Redis
  error it falls back to memory for that call, so limits never fail open.
* **Escalation:** at a cooldown >= `ESCALATE_MIN_SECONDS`, call `security_events.record(ip, ThreatCategory.AI_ABUSE,
  severity, detail, method="AI", path=f"/ai/{feature}", action="blocked", session_hash=...)` (action must be
  10 characters or fewer) plus `threat_tracker.add(ip, severity)` and `request_ban(...)` if the score reaches
  `threat_ban_score`. The usual `ban_duration` policy then applies: subscribers 2-24 h, repeat offenders without
  a subscription permanent. Whitelisted IPs are never banned. Gym Wi-Fi NAT means only sustained abuse may
  reach the IP level.
* **Tests:** `register_reset(guard.reset)` clears memory, and the Redis keys under this process's namespace.

## 4. The queue (app/ai/scheduler.py)

```python
scheduler: AIScheduler   # module singleton; lanes 'llm' (settings.ai_queue_concurrency) and 'tts' (ai_tts_concurrency)

async def acquire(self, owner: AIOwner | None, feature: str, *, max_wait: float | None = None) -> Ticket
    # Waits for a slot. Per-user fairness (round-robin between users inside a priority), priorities
    # REALTIME > INTERACTIVE > BACKGROUND with ageing (background work waiting > 60 s is promoted one level).
    # Limits: settings.ai_queue_max_per_user (queued+running per user, AIRejected('too_many')),
    # MAX_RUNNING_PER_USER, settings.ai_queue_max_total (AIRejected('busy')). A wait longer than
    # max_wait (default policy.max_wait; jobs wait as long as needed) raises AIRejected('busy').
    # Cancelling the waiting task removes the ticket. Publishes 'ai.queue' WS events (section 6) for user
    # tickets that wait longer than ~0.8 s.
def release(self, ticket: Ticket, *, outcome: str) -> None   # 'done' | 'failed' | 'cancelled' | 'expired'
def attach(self, ticket: Ticket, task: asyncio.Task) -> None # the child task doing the provider work
def cancel(self, *, user_id=None, page_id=None, session_hash=None, ticket_id=None, reason: str,
           kinds: set[str] | None = None, policies: set[str] | None = None) -> int
    # Cancels matching queued tickets (their waiters raise AICancelled(reason)) and running tickets (their
    # attached task is cancelled with msg=reason). Filters are ANDed; at least one id filter is required.
    # kinds filters AIOwner.kind ('user', 'job'), policies filters the ticket's exit policy. Returns the count.
def snapshot(self) -> dict   # {'lanes': {...running/queued...}, 'by_priority': ..., 'users': top waiting,
                             #  'counters': submitted/started/completed/failed/cancelled{reason}/expired/rejected{code},
                             #  'avg_wait_ms': ..., 'saved': {cancelled_running, cancelled_queued}}
```

### Integration in `AIRouter` (app/ai/client.py)

`complete()` and `stream()` gain keyword-only `user_text: str | None = None` (the raw user input, used only by
the guard) and `owner: AIOwner | None = None` (default `current_owner()`). Flow:

1. `admission = await guard.admit(owner, feature, user_text)`. Rejections propagate before any image prep or
   provider call.
2. `ticket = await scheduler.acquire(owner, feature)`. Queue time is never a provider failure, and provider
   timeouts start only after the provider semaphore is acquired.
3. The provider loop runs in a **child task** attached to the ticket. While it runs, a watcher polls
   `owner.disconnected()` every 0.5 s (HTTP requests only) and calls `scheduler.cancel(ticket_id=...,
   reason="client_left")` on disconnect. If the child was cancelled by the scheduler while the calling task
   itself was not (`asyncio.current_task().cancelling() == 0`), raise `AICancelled(reason)`, never a bare
   CancelledError (a cancelled handler means uvicorn ERROR logs and a DB session that isn't rolled back). If
   the caller itself is cancelled (SSE disconnect, shutdown), cancel the child and re-raise CancelledError.
4. `finally`: `scheduler.release(...)`, `guard.settle(admission, ok=...)`, and a `CallLog` with `user_id`,
   `owner_kind`, `queue_ms` and `cancelled` (appended synchronously).

Also fixed in the client: `OpenAICompatProvider.stream_text` closes the OpenAI `AsyncStream` in a `finally`, so a
closed generator aborts the gateway request instead of leaking it until GC. The Anthropic provider gets a real
`stream_text` (`beta.messages.stream`) so a cancel closes the HTTP stream. `stream()` honours
`ProviderError.availability`.

## 5. Presence and exit (app/services/presence.py)

```python
presence: Presence   # module singleton

def connected(self, user_id: int, *, conn_id: str, page_id: str | None, session_hash: str | None) -> None
def disconnected(self, user_id: int, *, conn_id: str, page_id: str | None, code: int | None) -> None
def touch(self, user_id: int) -> None          # HTTP activity (cheap; only acts when the user was 'away')
def page_left(self, page_id: str, *, user_id: int, reason: str) -> int   # immediate, page-scoped cancel
async def logout(self, user_id: int, *, session_hash: str | None, reason: str = "logout") -> None
def is_away(self, user_id: int) -> bool
def begin_shutdown(self) -> None               # lifespan: disconnects from now on are not exits
```

* **Page closed:** the client closes its socket with **code 4001** on `pagehide`, and `/ws?page=<page_id>` tells
  the server which tab it was. Then `scheduler.cancel(page_id=..., kinds={"user"}, reason="page_closed")` runs
  at once: a closed tab's interactive AI stops immediately.
* **User left:** the last socket of the user closes (any code but 1012/shutdown). After
  `settings.presence_grace_seconds` (covers page navigation and reconnect back-off) with no new socket in this
  process **and** no live lease in Redis from another process, `on_left(user_id)` runs:
  `scheduler.cancel(user_id=..., kinds={"user"}, reason="left")` and `jobs.apply_exit_policy(user_id)`.
* **Logout / revoke-all / account deletion / someone else signing in on the browser:** immediate. Cancel that
  session's tickets (in every process: the PG bridge carries the close request), and if the user has no other
  socket here and no other live session (the database decides, whichever process serves it), treat it as an exit
  right away (no grace period). Account deletion cancels every job of the user, whatever its policy.
* **Return:** a new socket (or authenticated HTTP activity) for an 'away' user calls
  `jobs.resume_user_jobs(user_id)`. A return noticed by HTTP comes before the page's socket: its 'resuming now'
  notes wait for that socket.
* **Database overloaded or unreachable** (pool timeout, refused or lost connection): the database step of a return,
  an exit or a sign-out is deferred - one warning a minute, no traceback - and retried by the presence sweeper
  (`retry_deferred`: 5 s doubling to 60 s, one probe a round), each retry judged on the state of then (a resume
  unless the user left again, an exit unless they came back). Never dropped. A socket whose session check meets such
  a database is closed with **1013** and comes back after a jittered back-off (section 7).
* **Cross-process:** each process keeps a Redis lease `ip:<env>:<db>:presence:<uid>` (ZSET of process ids
  scored by expiry, refreshed every 15 s while that process has sockets for the user). A grace timer only fires
  `on_left` when no other process holds a lease. Without Redis a single process judges by its own sockets; several
  processes (`REALTIME_PG_BRIDGE`, `manage.py run --workers N`) without Redis, or while it is down, fail safe: no
  process treats a closed tab as an exit, because the user's next page may be connected to another one (only
  sign-out and revocation end their AI work then). Each worker says so in its log at startup - run several workers
  with `REDIS_URL` set.

## 6. Background jobs (app/services/jobs.py)

* `job_handler(kind, *, on_exit="keep")` registers each kind's exit policy. `enqueue(..., user_id=None,
  exit_policy=None)` stores the requester and policy (default = the kind's). `user_id` stays optional;
  system work passes None.
* `run_due_jobs` runs up to `settings.jobs_concurrency` jobs at once, each in its own task registered by job
  id, with an `AIOwner(kind="job", user_id, job_id, exit_policy)` set. It still awaits everything it claimed
  before returning (tests rely on that). Final status updates are guarded with `status == RUNNING`. A
  `CancelledError` caused by an exit policy writes CANCELLED or PAUSED (attempts - 1, so a resume isn't
  charged) in a shielded session. A shutdown cancel puts the row back to QUEUED.
* `apply_exit_policy(user_id, *, reason) -> dict`: QUEUED/RUNNING jobs with policy `pause` become PAUSED (running
  ones are cancelled locally, or by the watchdog in other processes); policy `cancel` become CANCELLED;
  `keep` is untouched.
* `resume_user_jobs(user_id) -> int`: PAUSED becomes QUEUED, run_after=now.
  `cancel_user_jobs(user_id, *, reason, all_policies=False)`.
* Before running a claimed job, re-check its status and skip it if it is no longer RUNNING. If its user is away
  and the policy is pause/cancel, apply the policy instead of running it.
* A watchdog loop (every 2 s) heartbeats `locked_at` of locally running jobs (no more 15-minute double runs)
  and cancels local tasks whose row was CANCELLED/PAUSED by another process.
* Policies: `health_ai_note` pause; `coach_plan` (new, plan generation moved off the request) pause;
  `health_ac_note` (new) pause; `payroll_explain`, `verify_identity`, `verify_branch`, `verify_payment_proof`
  and `staff_activation_check` keep.

## 7. Wire protocol (server <-> IP.ai)

| What | Contract |
|---|---|
| Tab identity | `IP.pageId` (random per document). Sent as `X-Page-Id` on every `IP.api`/`IP.ai` request, as `page_id` hidden field on POST forms, and as `/ws?page=<id>`. |
| Leaving | `pagehide`: abort this page's AI requests and close the socket with **4001** (no reconnect). `pageshow` with `persisted`: new page id, reconnect. Never cancel on `visibilitychange` (live workouts lock the phone). |
| Refusal JSON | 429/409/422/503 `{"error": title, "detail": message, "code": quota\|cooldown\|too_many\|duplicate\|spam\|busy\|disabled, "retry_after": seconds, "ai_available": true}` plus a `Retry-After` header. `IP.api` sets `err.code` and `err.retryAfter`. Clients never auto-retry a 429. |
| SSE extra events | `{"type":"queued","position":n,"eta":s}` while waiting; `{"type":"blocked","code":...,"detail":...,"retry_after":n}` instead of an answer (then `done` with `"blocked": true`); `"busy"` falls back to the deterministic answer as today's `ai_down` path does. |
| WS events | `ai.queue {page_id, ticket, feature, state: queued\|running\|done\|cancelled\|expired, position, eta}`; `task.update {task_id, kind, status: queued\|running\|paused\|cancelled\|done\|failed, progress, message, link}` for background tasks. Clients filter `page_id === IP.pageId` where present. |
| Close codes | 4001 page hidden/closing (client), 4401 no session / signed out, 4403 bad origin/banned, 4409 signed out because the account signed in on another device (no reconnect; the page says so and goes to the sign-in page), 4429 flood, 1013 try again later (the server's database is overloaded: the page reconnects after a jittered back-off - 1-3 s, 2-6 s ... at most 10-30 s; a `hello` starts it over). |

## 8. Admin (`/superadmin/ai`)

An "AI usage & abuse" panel: queue snapshot (running/queued per lane and priority, average wait, cancelled-by-exit
counters = compute saved), top users over 24 h (`AICallLog.user_id`), active cooldowns with **Lift**
(`POST /superadmin/ai/cooldowns/{user_id}/lift`, audited `ai.cooldown_lift`), and recent blocks
(`SecurityEvent` category `ai_abuse` plus RATE_LIMIT events whose detail starts with "rate limit 'ai'"). The live
coach's chat messages are in the per-address `live` bucket (section 10, "Per-address rate limits"), so a flood of them
is recorded as "rate limit 'live'".
`CATEGORY_LABELS` gains `ai_abuse`.

As built: the panel is the `#usage` section of `/superadmin/ai`. **Lift** clears the cooldown, strike points and
duplicate fingerprints but keeps quota usage, writes the audit row only when something was cleared, and redirects
back to `/superadmin/ai#usage`. The queue, presence, compute-saved and running-job figures describe the process
that rendered the page. Cancelled calls (`AICallLog.cancelled`) are shown as "Stopped" and never count as provider
failures or towards the error rate.

## 9. Rules for implementers

* Keep the app importable at every step (two live servers hot-reload this tree). Import new modules lazily
  inside functions where a cycle is possible.
* No hard-coded limits outside `app/ai/policy.py`. No per-request DB queries in the guard's hot path except the
  cached entitlement lookup.
* Never block or cancel safety flows: the live coach's red-flag/discomfort handling, `health_metrics`, wellness
  red flags, identity/branch/payment verification.
* Tests must stay green: `tests/test_spoofing.py` crawls every route as 5 attacker identities (new endpoints must
  only ever touch the caller's own session/pages), `tests/test_csp_static.py` (no inline JS/style), query-count
  budgets on pages, and the existing per-feature suites.
* Frontend changes take effect after `python scripts/build_assets.py --js-only` (the dev watcher rebuilds
  automatically).

---

## 10. As built: refinements to the contract above

These were decided during implementation; they are backward compatible with sections 1-9.

**Guard (app/ai/guard.py)**
- The common admit path is one pipelined Redis round trip (cooldown read + duplicate claim + window charges),
  about 0.6 ms. The only database access is the cached AI-entitlement lookup (one EXISTS, 300 s cache,
  `guard.forget_entitlement(user_id)` after subscription changes).
- Refusals that happen *because* of a cooldown earn no strike points (otherwise retries during a 2-minute pause
  would escalate it to 24 h). A refusal whose strikes start a cooldown is raised as `cooldown` with that wait.
- Short replies (normalised text under 12 characters: "ok", "yes") use a 5-second duplicate window, so
  conversations stay natural; double-submits are still caught.
- Spam shape rules count Unicode letters and combining marks as letters (Hindi, Tamil, Arabic and Chinese text
  pass), ignore filler runs of `. - _ =`, and judge character variety over at most 120 characters, so long
  sincere paragraphs never trip it.
- Queue `too_many` refusals are fed back through `guard.refusal(...)`, so flooding the queue earns strikes too.
- `settle(ok=False)` refunds only into windows that are still open and never creates negative or immortal counters.

**Queue (app/ai/scheduler.py)**
- Tickets of background jobs always run at BACKGROUND priority, whatever feature they call (ageing still lifts
  them after 60 s), so a member's notes never compete with people waiting on screen.
- `cancel()` without an explicit `policies` filter never stops work whose exit policy is `keep` (safety reviews,
  verification, payroll, security). Account deletion passes `policies={'keep','pause','cancel'}`.
- Per-user limits are counted per lane (LLM and TTS are independent). Safety features are never refused by the
  queue and skip the per-user running cap.
- A cancelled stream stops at once, not at its next chunk: `Ticket.on_cancel` interrupts the wait for the provider.
  Before the first chunk the caller gets `AICancelled`; afterwards the stream ends with `meta['cancelled']` set.
- Waiting for a provider slot longer than the call's timeout raises `ProviderBusy` and fails over **without**
  marking the provider as failed; queue waits never open a circuit breaker.
- `OpenAICompatProvider.stream_text` closes the SDK stream, and the Anthropic provider streams for real
  (`beta.messages.stream`), so stopping a stream stops generation instead of leaking it until garbage collection.

**Presence, sockets and jobs**
- `/ws` hello carries `conn_id` and `page_id`; at most 16 sockets per user per process (4429). A 4429 makes the
  client back off at least 30 s.
- `hub.close_sessions(hashes)` closes the sockets bound to revoked sessions (logout, "sign out other devices",
  password reset, account deletion). Control events (`_ctl*`) never reach browsers.
- `realtime.call_after_commit(db, fn)` runs code only after a real COMMIT (dropped on rollback).
- Presence keeps an "away" flag in Redis (7-day TTL) next to the per-process leases. Any fresh arrival (first
  socket in a process, or HTTP activity via `presence.touch` while away) resumes paused jobs.
- HTTP activity during the grace period restarts it: `presence.touch` re-arms a pending exit, which then happens
  `presence_grace_seconds` after the user's last request, so someone whose WebSocket keeps failing while they keep
  browsing is not treated as gone (stat `kept_by_http`).
- A return over HTTP to another process is noticed too: `touch` asks Redis at most once per
  `presence.AWAY_RECHECK_SECONDS` (30 s) per user per process, never on every request. A set away flag means another
  process saw them leave, so it is deleted and their paused jobs resume (stat `away_checks`). An exit whose flag
  vanished while it was pausing jobs (a return elsewhere overtook it) resumes them at once (stat `exits_overtaken`).
- `job_handler(kind, on_exit=None)` falls back to `DEFAULT_EXIT_POLICIES` (pause: `health_ai_note`,
  `coach_plan`, `health_ac_note`; keep: `payroll_explain`, `verify_identity`, `verify_branch`,
  `verify_payment_proof`, `staff_activation_check`).
- Final job writes are fenced on `status == RUNNING` and the claim token, so a cancelled or paused job can never be
  overwritten as done. Stopped runs give their attempt back.
- The watchdog heartbeats running jobs every 2 s, so long jobs no longer run twice after 15 minutes, and it stops
  local runs whose row was paused or cancelled by another process.
- `task.update` events: `paused`, `cancelled`, `queued` with `resumed: true`, `done`, and `failed` once no retry
  is left. Kinds with policy `keep` add `notify: false`, because their own notifications already announce the
  result.
- A job handler never publishes its own final `task.update`. `jobs.set_result_event(**fields)` (`title`, `message`,
  `link`, `notify`, ...) goes into the runner's `done`, published after the DONE write. `jobs.set_failure_event(**fields)`
  goes into the `failed` sent once no retry is left; the `done` fields never go with a failure. Both work at any depth
  below the handler (ContextVar), return False outside the runner, and refuse `type`, `task_id`, `kind` and `status`.
  `jobs.current_run() -> JobRun | None` gives `job_id`, `kind`, `user_id`, `attempt`, `max_attempts`, `last_attempt`
  and `stopping` (the runner is pausing, cancelling or re-queueing the run, so it is not a failure).
  `health_ai_note`'s `done` and `health_ac_note`'s `done`/`failed` carry `notify: false`; `coach_plan`'s `done`
  carries `link: /me/coach`, its message and `notify: false`.

**Frontend (00-core.js, 01-ai.js)**
- `X-Page-Id` is only sent to same-origin URLs. `IP.apiError(res, data)` and `IP.parseRetryAfter(value)` are
  shared helpers.
- `IP.ai.request/stream` also accept `timeout`, `keepalive` and `safety` (safety requests are never refused or
  aborted locally). `IP.ai.formatWait(seconds)` formats waits, and `ai.cooldown.end` signals the end of a
  cooldown.
- Cooldowns are kept per page in memory. A fresh page may ask once and be refused by the server again; that is
  intentional, so an admin "Lift" is never overridden by a stale local timer.
- Queue indicators listen to `ai.queue` events for this tab's `page_id` and expire after
  max(60 s, 2 x ETA + 15 s) without news.
- `IP.ai.noteRefusal(feature, code, seconds) -> bool` records a refusal that arrived inside a 200 answer (the live
  coach's `refused`) exactly like a 429 with that code and `retry_after`: a cooldown for `feature` (a policy name, or a
  URL whose path is the bucket), every feature when the code is `cooldown`, and the `ai.cooldown` event. `seconds` is
  read like Retry-After (number, numeric text or HTTP date, capped at 7 days); it returns false and records nothing
  without a wait, or without a feature unless the code is `cooldown` (then the event's `feature` is null, as at the end
  of an account pause), and always for code `duplicate` (a refusal of those words only, section 12). A shorter wait
  never shortens a running one.

**Per-address rate limits (app/security/ratelimit.py)**
- `POST /api/live/sessions/{id}/message` is in the `live` bucket with the workout events, discomfort reports, `end` and
  `POST /api/tts`. Most messages are deterministic commands, and the AI behind them is held to each member's `chat`
  quota by the guard, so they never spend the `ai` bucket that a whole gym behind one NAT shares.
- `RATE_LIMIT_AI_PER_MINUTE` defaults to 60 (was 20; burst 30) for the remaining AI endpoints (assistant, plans, shop,
  coach plan, wellness, intake). These limits are per address and only stop floods; per-user AI quotas live in
  `app/ai/policy.py`. A local `.env` that sets the old value keeps it until it is changed.

**Hardening (guard, queue and router API additions, all optional and backward compatible)**

Tests: tests/test_ai_hardening_core.py (each item has regression tests that fail without it).

1. *Dedupe scope.* `guard.admit(owner, feature, text=None, *, scope: str | None = None)`; `ai.complete(...,
   dedupe_scope=None)` and `ai.stream(..., dedupe_scope=None)` pass it through. The duplicate fingerprint covers
   (feature, scope, normalised text): `sha256("<len>:<scope>\n<text>")`, or `sha256(text)` without a scope, so
   unscoped fingerprints are unchanged (`text_fingerprint(text, scope=None)`, `clean_scope(scope)`). The scope is
   trimmed and `''` means none. Callers pass the person's raw words as `user_text` plus `dedupe_scope="gym:<id>"` /
   `"product:<id>"` instead of tagging the text (`"[gym N] ..."`); spam and injection checks then see only their
   words. `Admission.scope` records it. The router passes `scope=` to the guard only when one is given, so stand-in
   guards written as `admit(owner, feature, text=None)` keep working for unscoped calls (tests that replace
   `guard.admit` for scoped callers need a `scope=None` keyword).
2. *Truncation flag.* `ai.stream` sets `meta['truncated'] = True` whenever it cut an answer short after its first
   words: `total_timeout` used up (the visible `" …"` chunk, `client.CUT_MARKER`), the provider failing mid-answer
   (`" … (the answer was interrupted)"`, `client.INTERRUPTED_MARKER`), a queue cancel (`meta['cancelled']` is also
   set), or the consumer closing the stream early. A complete answer leaves the key absent, as does a stream that
   raised before its first words. Consumers test `meta.get("truncated")` instead of matching marker strings; the
   marker chunks themselves are unchanged. A total-timeout cut is charged, an interrupted answer (provider failure)
   is refunded, and an answer its requester stopped after the first words is charged (hardening round 2, section 12).
3. *Leaving while queued.* The HTTP disconnect watcher also runs while a signed-in request waits in
   `scheduler.acquire`: it polls `owner.disconnected()` every `client.DISCONNECT_POLL_SECONDS` (0.5 s), and on a
   disconnect `scheduler.cancel(ticket_id=..., reason="client_left")` drops the queued ticket, so the waiter raises
   `AICancelled('client_left')` at once (counted as `cancelled_queued`, refunded, never reaches a provider).
   Mechanism: `scheduler.acquire(owner, feature, *, max_wait=None, on_queued: Callable[[Ticket], Any] | None =
   None)` calls `on_queued(ticket)` once, synchronously, when the ticket has to wait (never when a slot is free; a
   raising hook is logged and ignored). `complete()` keeps that same watcher on the provider work afterwards;
   `stream()` stops watching once it runs (its response notices a lost client). Exit policy `keep` work is never
   watched. TTS watches the disconnect itself (app/services/tts.py, section 12).
4. *A quota group for the AC advice wording.* `health_ac` is group `notes` (cost 1, BACKGROUND, exit `pause`), so
   it never competes with coach plans in `gen`. Quotas (minute, hour, day): member_ai (6, 40, 120), member
   (3, 15, 40), staff and owner (6, 40, 120), superadmin (30, 200, 600). Refusal wording: "You've used your
   AI-written notes for this minute ...". `guard.status()['usage']` and `['limits']` include `notes`.
5. *OCR vision step guarded.* `ocr` is `guarded=True`, group `vision`, cost 1 (priority, `max_wait`, exit `keep`
   unchanged). Only a person's own request is judged and charged (e.g. the OCR engine call inside
   `POST /me/health/scan`); jobs (identity, branch, payment-proof and certificate verification) and system work are
   never judged. A refusal inside the OCR service is an engine failure ("AI unavailable"), not an exception.
6. *Entitlement freshness across processes.* `guard.forget_entitlement(user_id=None)` still forgets this process's
   cached tier at once and also bumps a shared version key, `ip:<env>:<db>:ai:entv:<uid>` (`entv:all` when
   forgetting everyone), in a task of its own; it returns an awaitable that waits for the bump, which synchronous
   callers such as `call_after_commit` may ignore. A cached entitlement stores the version read **before** its
   database lookup (after this process's own pending bumps landed). `admit` reads the version with an MGET inside
   the pipelined round trip it already makes, so the common path has no extra round trip. A different version means
   one fresh lookup in that same request, and the tier is re-recorded. A change committed while a lookup runs is
   therefore never cached as current. Version keys start from a random base and live 24 h. Without Redis the bump
   stays in this process's memory (process-local, as before).
7. *Refusal counters.* `await guard.snapshot() -> {'refused': {'quota': n, 'duplicate': n, 'spam': n,
   'cooldown': n}, 'cooldowns_started_today': n, 'escalations_today': n, 'day': 'YYYY-MM-DD', 'backend': 'redis' |
   'memory'}`, per UTC day. Keys are `ip:<env>:<db>:ai:stat:<day>:<name>`, kept 3 days and created with their TTL.
   Refusals are counted inside the round trips the refusal makes anyway: the strike pipeline, or the rollback for
   `cooldown`. A refusal whose strikes start a pause is shown to the person as `cooldown` but counted under its
   reason. `cooldown` counts requests that arrived during a pause. `cooldowns_started_today` counts new pauses,
   not extensions. `escalations_today` counts pauses reported to the security system (whitelisted or missing IPs
   are not). The queue's own refusals (`too_many`, `busy`, expired) stay in `scheduler.snapshot()`. With Redis the
   counts cover every process; otherwise the counts are this process's memory.
8. *Admission round trip for jobs.* `Admission.to_payload() -> dict` (JSON-safe: `v`, `ref`, `user_id`, `feature`,
   `group`, `units`, `tier`, `backend`, `origin`, `charges` as `[key, units, window_end]`, `dedupe_key`,
   `dedupe_token`, `fingerprint`, `scope`, `settled`). `Admission.from_payload(payload) -> Admission | None`.
   `await guard.settle_payload(payload, ok) -> bool`: `ok=False` refunds what the person was charged at the click
   (and forgets the fingerprint), for example when the job falls back without AI; `ok=True` keeps the charge.
   Idempotent across copies, retries and processes: the first settle claims `ip:<env>:<db>:ai:stl:<ref>` (SET NX
   in the store that holds the charge) and later ones do nothing. That includes `guard.settle(admission)` of an
   exported admission, which goes through the same claim. Returns True when this call settled it, marks the
   payload `settled` in place, and never raises. Malformed payloads, or payloads naming keys outside this
   namespace or of another user, are ignored. A charge kept in another process's memory (no Redis) cannot be
   reached and is left alone.

**Presence hardening (sessions, several workers, close requests, the resume note)**

Tests: tests/test_presence_hardening.py (each item has regression tests that fail without it).

- *A sign-in replaces the browser's session.* `sessions.revoke_session(db, token, *, end_live=True)` ends the revoked
  session after the COMMIT like every other revocation: its sockets close with 4401 (here and, through the PG
  bridge, in other processes), its AI work stops, and with no other live session of that user it is their exit
  (reason `signed_out`). So when someone else signs in on a browser where another account was signed in
  (`POST /auth/invite`, through the session rotation in `auth_flows.start_session`), the previous user's open tabs
  stop receiving their live events. Sign-out passes `end_live=False`: it ends the session itself, awaited, with
  reason `logout`.
- *Sign-out is decided by the database.* `presence.sessions_ended` no longer consults the Redis leases: another
  process's lease may exist only because of a socket of the session that just signed out (a second tab of it). With
  no other live session (`user_sessions`) and no socket of another session here, it is an exit at once.
  `hub.close_sessions(hashes, *, code=4401, reason="signed out", user_id=None, exit_reason=None)` sends `user_id` and
  `exit` over the bridge. A process receiving it closes its sockets of those sessions and calls
  `presence.sessions_ended_elsewhere(user_id, hashes, reason=..., sockets_closed=n)`: that session's AI requests stop
  there at once and, if the process held anything of the user (a socket or AI work of those sessions), it judges
  the exit too, so the closing socket starts no grace timer. `hub.cancel_ai(user_id=..., reason=..., policies=...)`
  cancels a user's AI requests in every process (account deletion passes every policy, including `keep`).
- *Several workers without Redis fail safe.* `presence.multi_process()` is `settings.realtime_pg_bridge` (set by
  `manage.py run --workers N`). In that mode, when Redis is not configured or does not answer, a process cannot see
  the others' sockets: a grace timer never declares an exit (stat `kept_unseen`), and the job runner's away check
  trusts a local away mark only while the user is signed in nowhere (they may have come back through another
  worker). Sign-out, revocation and a closed page's own cancel (4001) still work. `presence.startup_check()` runs when
  the PG bridge starts and logs a warning in that case. A single process without Redis behaves as before.
- *A parked job never stays paused for someone who is present.* The runner reads `away_now` and then writes PAUSED;
  a return in between resumed nothing, because the row was still RUNNING. So once the PAUSED row is written the
  runner calls `presence.resume_if_back(user_id)` (under presence's per-user lock): if the user is back by then, their
  paused jobs resume (stat `parks_overtaken`). A return after that finds the row PAUSED and resumes it itself.
- *Close requests are never dropped.* A socket's queue is a 256-item drop-oldest buffer. When a socket is asked to
  close, the events still waiting for it are dropped and nothing more is delivered to it, so its `CloseRequest` is
  the only item left and no later event of the account can push it out. A client that stops reading (for example
  with a stolen cookie) can no longer keep a revoked socket streaming.
- *The 'resuming now' note reaches the page.* A return noticed by an HTTP request (`touch`, or its Redis re-check)
  comes before the page's socket. `jobs.resume_user_jobs(user_id, *, announced=None)` hands the published
  `task.update {status: queued, resumed: true}` events to presence, which keeps them for the user's next socket for
  `presence.NOTE_KEEP_SECONDS` (60 s). They are kept in Redis (`ip:<env>:<db>:presence:notes:<uid>`, read and deleted
  in the arrival's existing round trip, whichever process the socket lands in), or in this process without Redis. A
  socket-first return gets the events live, once.

## 11. Feature migration (as shipped)

Every AI feature that takes typed or spoken words passes them (never the prompt) as `user_text`, and every feature
lets refusals reach the person; none of them turns `AIRejected` into its "AI offline" fallback any more. Code `busy` (queue full / waited too long) keeps each
feature's deterministic answer, worded "AI is busy", and is refunded. A refusal never runs the side effects the AI
answer would have caused.

| Feature (endpoint) | Refusal (quota, cooldown, too_many, duplicate, spam) | Busy | Requester left (`AICancelled`) |
|---|---|---|---|
| Assistant (`POST /api/assistant/chat`) | contract JSON 429/409/422 + Retry-After; nothing executed or stored | basic-command answer with `"busy": true` | 503 `{code: "cancelled"}`; the answer is re-checked against `owner.disconnected` before any action runs |
| AI intake (`POST /owner/customers/intake`) | always JSON (whatever the Accept header) + Retry-After + `Cache-Control: no-store` | regex prefill; the 200 answer always carries `"busy": bool` | 503 `{code: "cancelled"}` |
| Plan questions (`POST /api/plans/ask`, SSE) | `{"type":"blocked",code,detail,retry_after}` then `done {blocked: true}` | standard answer, `busy: true` in meta and done | the stream ends without `done`; nothing cached |
| Product questions (`/api/shop/products/{id}/ask[/stream]`) | stream: `start`, `blocked`, `done {blocked}`; JSON: contract JSON | listing answer with `busy: true` | stream ends; JSON 503 `cancelled` |
| Live coach (`POST /api/live/sessions/{id}/message`) | HTTP 200 with a spoken line + `"refused": {code, detail, retry_after}`, logged as event `ai_refused`; the browser hands it to `IP.ai.noteRefusal("live_coach", ...)` (its wait is IP.ai's cooldown, `cooldown` pauses the page's other AI widgets and the natural voice, a duplicate pauses nothing); `"ai": false` runs safety + commands only | smalltalk fallback + `refused.code = "busy"` | `{reply: "", cancelled: true}` |
| Coach voice (`POST /api/tts`) | 429 contract JSON + `fallback: "browser"` (quota/cooldown); 503 `{fallback, code, retry_after}` (too_many/busy). Cache and syntheses are per member (`tts.requester_scope`), and a refusal reaches only the member it belongs to | browser voice | an aborted fetch is noticed within 0.5 s and leaves its synthesis; it stops (ticket released) once every waiter is gone - free while it waited for a voice-lane slot, charged once Kokoro worked on it, never struck |
| Coach plan (`POST /me/coach/generate`) | `guard.admit(owner, "coach")` at the click: redirect + flash with the wait, nothing queued | template plan, click refunded | `coach_plan` job (pause): paused while away, resumed on return; progress via `task.update` and `GET /me/coach/status?job=` |
| AC advice wording | `guard.admit(owner, "health_ac")` at the click; a refusal silently keeps the rule-based wording | - | `health_ac_note` job (pause), one per member |
| Health note of a normal reading (`POST /me/health/metrics`) | only for a plan that includes AI (others keep the deterministic note, no AI call); `guard.admit(owner, "health_note")` at the post, a refusal silently keeps the deterministic note and queues nothing | deterministic note, click refunded | `health_ai_note` job (pause), one per reading; refunded when no AI note comes of it |
| Scale scan (`POST /me/health/scan`) | OCR reading + "AI vision check skipped" on the result card | silent OCR fallback | OCR reading, no message |
| Wellness selfie (`POST /me/wellness`) | check-in saved, `face_analysis.status = "refused"` + flash with the wait | "unavailable" as before | status `cancelled`; red-flag check-ins never read the selfie (`skipped`) |
| Health metrics review (`POST /me/health/metrics`, `/confirm`) | never refused by the guard (safety), policy keep; at most `health.REVIEWS_PER_DAY` (10) reviews per member and UTC day (`guard.reserve`, race-safe) - past it the rules decide alone and the note says the AI review was not repeated; the rules' outcome (pause, notices) is committed first and the review runs with no transaction open; a reading is confirmed (and reviewed) once, however many confirmations arrive at once | the rules' outcome and deterministic note; the allowance is given back | never cancelled |
| Super-admin provider test (`POST /superadmin/ai/test`) | unguarded; queue refusals: JSON contract or a warning flash, no audit row | - | 503 `cancelled`, not recorded as a failed test |

Notes:
- Q&A features tag `user_text` with their subject (`"[gym 12] Which plan?"`, `"[product 7] ..."`) so the same
  suggested question about a second gym or product is not a duplicate (the duplicate check is per user + feature).
- A stream cut short by a provider failure (`" … (the answer was interrupted)"`) is settled as not ok: refunded, and
  asking again at once is not a duplicate. Cut answers are never cached (plans, shop match the marker chunks).
- A requester who leaves mid-answer (stream: after the first words; completion: once a provider slot was held) is
  charged, and the same question may be asked again at once (charged again). Leaving earlier is free. Repeated drops
  earn strike points (section 12).
- The scale scan and the wellness selfie commit before OCR / the AI vision call; the coach plan and health note jobs
  commit before the AI call. No transaction or pooled connection is held while a model works.
- `POST /owner/staff/{id}/recheck` reads unmatched certificates again through `ocr.reread_documents`: at most once per
  10 minutes per staff record, 3 re-reads per certificate, 3 certificates per request (newest first), one `vision`
  unit each, charged to the owner who clicked.
- Plans and shop close the request's DB session before waiting in the AI queue, and close their event generator when
  the response ends (`EventStreamResponse` in app/routers/plan_ai.py), so a disconnect releases the ticket at once.
- The shop's old per-process ask throttle is gone; the guard's per-user `qa` quotas apply (shared with plans).
- Coach plan charge: `queue_plan` stores the click's `Admission.to_payload()` as `payload["admission"]`, and `plan_job`
  settles it with `guard.settle_payload`. The charge is kept when the AI answered (`ai_status` `ok`, or `rejected`
  when its plan failed the safety filter). It is refunded when the plan is the template because the AI was offline or
  busy or the membership no longer includes AI, when nothing was built (suspended, membership ended, account gone),
  and when the last attempt fails. A paused run keeps the charge for the resumed run.
- `guard.forget_entitlement(customer_id)` runs after the COMMIT of `activate_subscription`, `cancel_subscription`,
  `expire_due_subscriptions` (app/services/checkout.py) and account deletion (app/services/accounts.py).
- Forms posted by components that handle their own busy state through `IP.ai.request` carry `data-no-busy`
  (00-core.js would otherwise leave the button disabled after a local-cooldown refusal).

## 12. Hardening round 2

Tests: tests/test_ai_hardening_final_core.py (guard, router, queue), tests/test_ai_frontend_static.py (IP.ai in
JavaScriptCore), tests/test_presence_hardening.py (presence, sessions, sockets, jobs), tests/test_ai_hardening_features.py
(health note, coach voice, staff re-check, transactions) and tests/test_ai_hardening_integration.py (the cross-module
wiring). Each change has regression tests that fail without it.

**What members see differently**

- *Leaving mid-answer is no longer free.* An answer is charged once a provider worked on it, however it ends: a stream
  (plan and product questions) after its first words went out, a completion (assistant, live coach chat, intake) once
  its provider slot was held - closed tab, dropped connection, closed sheet, a sign-out or exit that lands mid-answer.
  Still free: a refused request, a busy queue, anything stopped while queued or while waiting for a provider slot, a
  stream stopped before its first words, and a call every provider failed (a stream cut by a provider failure
  included). A cut answer forgets its duplicate fingerprint, so the same question may be asked again at once (a new,
  charged question). Server-side deadlines (the live coach's 9 s turn, a health review's `wait_for`) are charged once a
  provider slot was held but never struck; a caller that wants a refund for its own deadline bounds the attempt with
  `ai.complete(timeout=...)` instead (provider timeouts are failures, refunded).
- *Dropping answers again and again pauses AI.* A drop mid-answer whose reason is `client_left`, `page_closed` or
  `aborted` (`client.ABORT_REASONS`) is noted by `guard.note_abort`: the first `policy.ABORT_FREE` (5) within
  `STRIKE_WINDOW` (15 min) are free, each one after that adds `STRIKE_POINTS['abort']` (1). It is never a refusal and
  never part of a flood burst, but the points climb the usual cooldown ladder. New pause wording: "AI is paused on your
  account for N because of AI answers being abandoned half-way again and again." Drops are counted in
  `ip:<env>:<db>:ai:ab:<uid>`; the admin's **Lift** clears that counter too.
- *Any text is judged.* `ai.complete`/`ai.stream` run `guard.clean_text` once on `system`, `prompt`, `user_text` and
  `dedupe_scope`: lone UTF-16 surrogates (a JSON `"\ud800"` escape) become U+FFFD before the guard and before any
  provider SDK. The guard cleans its own input and, should reading the words still fail, judges the request without
  them - cooldown and quotas always apply (a paused account stays paused whatever it sends). A provider's
  `UnicodeEncodeError` is the request's error (`availability=False`) and never opens a circuit breaker, so one person's
  bad input can no longer take AI offline for everyone.
- *A duplicate refuses those words only.* A 409 or SSE `blocked` with code `duplicate` no longer pauses the feature in
  the browser and emits no `ai.cooldown` event. `IP.ai` remembers the refused words (normalised like the server's
  `normalize_text`, per feature and URL path + query) for the server's `retry_after`: sending them again in that window
  is refused locally with the server's shape (`{status: 409, code: "duplicate", retryAfter, local: true}`, "You just
  asked that. Try again in N s."), anything else is sent at once. The assistant's send button and the intake's "Fill
  with AI" stay enabled. `IP.ai.noteRefusal(feature, "duplicate", s)` returns false and records nothing.
- *Health note of a normal reading.* Only a membership whose plan includes AI gets the AI-written note (checked at the
  post and again when the job runs); everyone else keeps the deterministic note, with no AI call at all. The post is
  judged as feature `health_note` (`policy.py`: group `notes`, cost 1, BACKGROUND, exit `pause`, no duplicate window) -
  a friendly note, never the `health_metrics` safety review. A refusal silently keeps the deterministic note and queues
  nothing; posts while the reading's note job is queued, running or paused take no second charge. The admission rides
  in the job payload (`payload["admission"]`) and is kept when an AI note is written, refunded otherwise (AI offline,
  plan lapsed, reading logged again meanwhile - that note is dropped -, failure).
- *Coach voice is private.* `/api/tts` caches and synthesises per member (`tts.requester_scope(owner)`: `user:<id>`):
  another member's line is an ordinary miss (judged, charged, synthesised), so nobody can learn from status codes,
  timing or counts which lines (names, workout details) someone else heard. A queue refusal reaches only the member whose
  ticket was refused (all their tabs). `GET /api/tts/status`, refusal bodies and the live page config no longer carry
  `cached` (`tts.status(admin=True)` keeps the counts for the admin page). A voice line whose fetch the coach page
  aborted is noticed within `client.DISCONNECT_POLL_SECONDS` (0.5 s): it leaves its synthesis, and once nobody waits
  the ticket is released - no `too_many`, no strikes for the next line. A line dropped while it waited for a
  voice-lane slot is refunded; once Kokoro worked on it, it is charged like `ai.complete` (`_Flight.speaking`), so
  aborting lines mid-synthesis is no way around the voice quota - interrupting a line is ordinary coach behaviour,
  so it is never struck. Generic cues ("Rest 45 seconds") are now synthesised once per member (200-line, 48 MB LRU).
- *Shared browsers.* When someone else signs in on a browser where another account was signed in, the previous
  account's open tabs are closed (4401) and stop receiving its live events, its AI work stops, and with no other live
  session it is that user's exit (reason `signed_out`). A sign-out is immediate even when a tab of that session is
  connected to another worker, which stops that session's AI work at once.
- *A revoked session's socket closes.* Events still queued for a socket that is asked to close are dropped and nothing
  more is delivered to it, so a client that stops reading can no longer keep a revoked `/ws` streaming.
- *Coming back.* A job the runner parks while its requester is returning resumes (it is never left paused for someone
  who is present), and the "Paused while you were away - resuming now" note reaches the page also when the return was
  noticed by an HTTP request (kept 60 s, `presence.NOTE_KEEP_SECONDS`, for the next socket).
- *Queue ids.* `ai.queue` events carry an opaque ticket id (`t` + 16 random hex digits); the old process-wide counter
  told members how much AI everyone else was using.

**What owners see differently**

- *Staff certificate re-check.* `POST /owner/staff/{id}/recheck` reads unmatched certificates again only within
  bounds (`app/services/ocr.py` `reread_documents`): at most once per `REREAD_COOLDOWN_SECONDS` (10 min) per staff
  record, `REREAD_LIMIT` (3) re-reads per certificate, `REREAD_BATCH` (3) certificates per request (newest first), and
  one `vision` unit (feature `ocr`) per certificate charged to the owner who clicked. A refused re-check clears and
  queues nothing and gives back what it charged; the owner sees a warning flash: "These certificates were read again
  N ago. You can ask again in M.", "Each certificate can be read again at most 3 times. Upload a clearer copy, or open
  it and approve it yourself if it is genuine.", or the guard's own quota / AI-paused wording. Before, every click
  re-ran OCR and the AI vision check on every unmatched certificate, unlimited and uncounted.
- *Intake.* A duplicate note no longer disables "Fill with AI"; a note sent with a lone surrogate is judged and
  answered like any other (a paused owner still gets 429 `cooldown`).
- *Account deletion* stops the person's AI requests in every worker (`hub.cancel_ai`, PG bridge op `cancel_ai`, every
  exit policy including `keep`).

**What admins see differently (`/superadmin/ai`)**

- Presence counters: "Paused jobs resumed as their requester came back" (`parks_overtaken`), "'Resuming now' notes
  kept · delivered" (`notes_kept`, `notes_delivered`) and "Exits not judged (workers without shared Redis)"
  (`kept_unseen`). The voice service line shows the per-member cache, hits and refusals (`tts.status(admin=True)`
  through `selfcheck.probe_tts`).

**New limits and settings** (all in `app/ai/policy.py` unless named)

| What | Value |
|---|---|
| `FeaturePolicy("health_note", "notes", priority=BACKGROUND, max_wait=120, on_exit=pause, dedupe_seconds=0)` | the AI note of a normal reading |
| `STRIKE_POINTS['abort']`, `ABORT_FREE` | 1 point per drop beyond 5 drops per 15 min |
| `ocr.REREAD_COOLDOWN_SECONDS`, `REREAD_LIMIT`, `REREAD_BATCH` | 600 s per record, 3 per certificate, 3 per request |
| `presence.NOTE_KEEP_SECONDS` | 60 s (Redis list `ip:<env>:<db>:presence:notes:<uid>`) |

**New refusal reasons**

- Pause reason `abort` (above). Codes are unchanged: a pause is still `cooldown`.
- Re-check refusals carry `Reread.code` `cooldown` (this record, just now), `limit` (read too often) or the guard's code.
- The browser's local duplicate refusal (`local: true`) repeats the server's 409 for those words only.

**Several workers need Redis**

`manage.py run --workers N` sets `REALTIME_PG_BRIDGE`; presence then needs `REDIS_URL` to see the other workers'
sockets. Without Redis, or while it does not answer, workers fail safe: closing every tab no longer stops a member's AI
work after the grace period (stat `kept_unseen`), and the job runner trusts a local away mark only while the member is
signed in nowhere. Sign-out and revocation (decided by the database, not Redis) and a closed page's own cancel
(4001) still stop AI work at once. Every worker logs a WARNING naming `REDIS_URL` at startup
(`presence.startup_check`), `manage.py run --workers N` prints a warning when `REDIS_URL` is not set, and
`scripts/check_env.py` says Redis is needed with several workers. A single process without Redis behaves as before.

## 13. What the member told the app once: fitness profile, memory, one member context

Code: `app/ai/member_context.py` (the context builder), `app/ai/memory.py` (remember / confirm / undo / forget),
`app/services/fitness_profile.py` (vocabulary, caps, race-safe writes), model `FitnessProfile` (`fitness_profiles`,
next to `customer_profiles` in app/models/users.py). Tests: `tests/test_ai_memory.py`.

**Data.** Health facts stay in `health_conditions` (condition / allergy / medication / injury, safety tag from the one
normaliser `intake.condition_tag`); the main plan goal and the experience level stay on `customer_profiles`. The
fitness profile holds exercise types the member enjoys and avoids, goals and equipment (vocabulary slugs or the
member's own short labels), sessions per week, minutes per session, preferred time of day, notes, and `forgotten` (what
they asked the AI to forget - hidden from its view of past chats). Pages: `/me/health/profile/fitness` (edit) and
`/me/health/profile/ai` ("What the AI knows about you": every item the AI uses, each with Edit / Forget, plus exactly
what a prompt is given).

**One context for every AI feature.** `load_member_context(db, member, viewer=None)` reads the member's profiles
(one outer join) and at most 50 health rows, safety-relevant first (`health.safety_first()`): two queries whatever the
member holds. `MemberContext.block(max_chars=...)` is deterministic and bounded (what does not fit is counted: "not
shown (size limit): N more"); everything the member typed is DATA - JSON-quoted, prompt delimiters (`<<<`, `>>>`,
`"""`) neutralised, and a saved text that looks like an instruction to the AI is withheld (`guard.instruction_marker`:
the chat injection phrases plus chat-format role markers - "SYSTEM:", "[assistant]", "<|im_start|>" -, new rules for
the AI, "always tell this member ...", overriding the rules; matched in a folded copy - NFKD, every zero-width /
format character and combining mark dropped, Cyrillic and Greek look-alike letters folded - of the whole saved text,
so neither invisible characters, full-width or look-alike letters, line breaks nor the length cut hide one; health
items, their notes, the fitness profile's own items and notes and the notes for the coach alike; notes in the
member's own voice - "tell me if my form is off", "you must keep my heart rate under 150" - stay). Strictly one member: any
`viewer` other than the member must be allowed to see their health data (`profiles.access_level` >= trainer), else
`PermissionError`. Nothing is cached - a removed item is gone from the very next prompt.

| Feature | Uses the context | Notes |
|---|---|---|
| Assistant (`POST /api/assistant/chat`) | MEMBER PROFILE block (1,300 chars) | forgotten items also hidden from RECENT / RELATED history |
| Plan questions (`/api/plans/ask`) | block (900 chars) | a personal prompt's answer is cached for that member alone |
| Product questions (`/api/shop/products/{id}/ask`) | the shop's safety-ordered rows + goals / likes / avoids / level | personal answers cached per member |
| Coach plan job (`coach_plan`) | block (1,100 chars); safety tags from the same rows | avoided exercise types never offered to the AI or the template; the template keeps the sessions per week |
| Live coach replies | SAVED PROFILE block (520 chars) + forgotten items out of the session history | |
| Live coach voice cues | deterministic: "Go gently on your knee - stop if it hurts."; the next exercise is re-checked against the CURRENT tags and swapped when a condition saved mid-session rules it out | |
| Wellness selfie, health note, health review, AC wording | block (700 chars) | |
| Owner intake | not applicable - a new member, no saved data; never another member's | |

**Memory from conversation.** The assistant and the live coach find durable facts the member states about
themselves ("I have asthma", "my left knee hurts on squats", "I only do yoga and swimming", "I can train 3 times a
week", "I am allergic to whey") with deterministic rules - so it works with every provider, the simulated one and an
offline or busy AI included - and, with a real model, from the assistant's `remember` list (accepted only when every
significant word is in the member's own message, from an allow-listed field, never instruction-like). Questions,
hypotheticals, negations, other people and task commands are never facts. A medication is remembered by its name and
an optional dose ("Metformin 500 mg") - never how often, when or why it is taken ("every morning", "at night", "for
my diabetes"); one saved with its schedule earlier already counts as known. An answer carries `memory: [{op, field,
label, detail, status, token}]`: `proposed` (a signed, member-bound ticket, 24 h), `known` or `full`. Nothing is saved
until the member presses Remember (`POST /api/memory/confirm {token}`: 200 `saved` + an `undo` ticket, `known` on a
second click; 409 `full` at a cap; 400 `expired` / `invalid`; members only). `POST /api/memory/undo {token}` reverses
a confirm or a forget. "Forget ..." in a chat (never instead of a red flag or a pain report in the live coach) or
Forget on the memory page removes the item at once, with an undo ticket, and keeps it out of the history the model
sees until it is added again.

**Caps (race-safe).** 50 health items per member, whoever adds them (the member's form, Remember, the gym's intake /
health edit - all behind `health.lock_conditions`, a per-member advisory lock; the gym's lists are refused above 50 in
total; one owner save adds at most 20 entries to a list, and is applied against the snapshot of the entries its form
showed - `intake.update_health`: an entry is never split, re-spelled or stripped of its note, one the member added
meanwhile is never removed, and without a snapshot the member's own entries are never removed). Health alerts: one
per reading and source, updated in place when the day's reading is logged again, and at most 20 new ones per member
per 24 h (`health.add_alert`; past it the newest of the same kind is updated and unread again - never dropped).
Fitness profile: 20 exercise types enjoyed, 20 avoided, 10 goals, 30 equipment items, own labels 40 characters
(goals 60), notes 500, sessions 1-14 a week, 10-240 minutes, 50 remembered "forget" requests - written behind the
row's `SELECT ... FOR UPDATE`, with CHECK constraints (50 per list) as the database backstop. Load safety in the same
area: 1,000 assistant messages kept per person (older trimmed on write), 30 live workouts started per 24 h (per-member
lock), 2,000 events logged per live session (safety events always), 20 plans generated per 24 h, at most 5 memory
offers per message and 10 items per "forget".
