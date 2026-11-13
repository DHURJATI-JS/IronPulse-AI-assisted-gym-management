<p align="center">
  <img src="docs/readme/logo.svg" alt="IronPulse" width="360">
</p>

<p align="center">
  AI-assisted gym management - gyms, staff and payroll for owners; plans, an AI coach with memory, a live voice coach,
  health safety checks, a gym shop and live chat for members.
</p>

<p align="center">
  <img alt="Python 3.10+, tested on 3.15" src="https://img.shields.io/badge/Python-3.10%2B%20%C2%B7%20tested%20on%203.15-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI 0.115+" src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white">
  <img alt="PostgreSQL 18 + pgvector" src="https://img.shields.io/badge/PostgreSQL-18%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white">
  <img alt="Redis, optional" src="https://img.shields.io/badge/Redis-optional-DC382D?logo=redis&logoColor=white">
  <img alt="Tailwind CSS v4" src="https://img.shields.io/badge/Tailwind%20CSS-v4-06B6D4?logo=tailwindcss&logoColor=white">
  <img alt="Runs on Windows, macOS and Linux" src="https://img.shields.io/badge/runs%20on-Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-555555">
  <a href="#testing"><img alt="Tests: 3,792 passing" src="https://img.shields.io/badge/tests-3%2C792%20passing-2EA44F?logo=pytest&logoColor=white"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-yellow"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#features-by-role">Features</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#ai">AI</a> ·
  <a href="#security--privacy">Security</a> ·
  <a href="#limits">Limits</a> ·
  <a href="#configuration-env">Configuration</a> ·
  <a href="#deploying-to-production">Deploy</a> ·
  <a href="#load-test-results">Load test</a> ·
  <a href="#disclaimer--no-liability">Disclaimer</a>
</p>

> [!WARNING]
> **Disclaimer - The owner, the developer and the AI are NOT responsible for anything; this is just a student-grade project; nothing here is medical, legal or financial advice.**
> Provided "as is", without warranty of any kind; AI can make mistakes. In an emergency, call your local emergency number (112 in India and the EU, 911 in the US, 999 in the UK) - never rely on this app. Full text: [Disclaimer & no liability](#disclaimer--no-liability) · [TERMS.md](TERMS.md) (in the app at `/terms`).

## Load test results

<!-- LOADTEST:START -->
**🗄️ Data volume: 2,086,466 database rows in 50 tables** - **22,857 users** · **308 gyms** · **2,016 branches** ·
**27,410 memberships** · **20,065 shop products** · **20,020 shop orders** · **102,850 transactions** ·
**112,106 chat messages** · **493,122 height & weight readings** - with random junk in 10 % of the values of 27 text
columns.

> ⚠️ Measured on one laptop with a simulated AI - not a performance guarantee. See [Disclaimer & no liability](#disclaimer--no-liability).

**Run `stress-20261113-135009`** · 13 Nov 2026, 13:55–14:05 IST · 10 minutes · production mode · simulated AI

| | | |
|:--:|:--:|:--:|
| ✅ **Overall: PASS**<br><sub>every production check passed</sub> | 📨 **168,057 requests**<br><sub>in 10 minutes, up to 1,600 simultaneous users</sub> | ⚡ **0.00 % errors · 22 ms p95**<br><sub>at 200 users, the step the SLOs are judged on</sub> |
| 📈 **700 users within the SLO**<br><sub>579 requests/s; the knee is at 1,050 users</sub> | 🧱 **0 crashes**<br><sub>0 HTTP 500s, 0 tracebacks, 0 worker restarts</sub> | 🔁 **Back to normal in 10 s**<br><sub>after a 1,600-user spike</sub> |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/readme/loadtest-p95-dark.svg">
  <img src="docs/readme/loadtest-p95.svg" width="760" alt="Page response time, 95th percentile, per load step on a log scale, with the SLO line at 1 second: 50 users 40 ms; 100 users 22 ms; 200 users (SLO step) 22 ms; 350 users 25 ms; 700 users (last step within the SLO) 207 ms; 1,050 users (knee) 2.4 s; 1,600 users (spike) 10.2 s; 200 users (recovery) 26 ms.">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/readme/loadtest-rps-dark.svg">
  <img src="docs/readme/loadtest-rps.svg" width="760" alt="Throughput in requests per second per load step: 50 users 47; 100 users 87; 200 users (SLO step) 170; 350 users 286; 700 users (last step within the SLO) 579; 1,050 users (knee) 635; 1,600 users (spike) 696; 200 users (recovery) 165.">
</picture>

| Users | Step | Requests/s | Page p95 | Errors | Within the SLO |
|---:|:--|---:|---:|---:|:--:|
| 50 | | 47 | 40 ms | 0.00 % | ✅ |
| 100 | | 87 | 22 ms | 0.00 % | ✅ |
| **200** | **SLO step** - every SLO is judged here | 170 | **22 ms** | **0.00 %** | ✅ |
| 350 | | 286 | 25 ms | 0.00 % | ✅ |
| 700 | last step within the SLO | 579 | 207 ms | < 0.01 % | ✅ |
| 1,050 | **knee** - the 4 workers' CPUs are full, requests queue | 635 | 2.4 s | 0.00 % | ❌ p95 over 1 s |
| 1,600 | **spike** - 1.5 × the knee within ~11 s | 696 | 10.2 s | 3.79 % | ❌ overloaded |
| 200 | **recovery** | 165 | 26 ms | 0.28 % | ✅ |

Requests/s and errors count every request the server answered: normal users, abusers and junk senders (the run's
report also breaks them down for normal users only: 508 of the 579 requests/s at 700 users).

**What overload looks like.** At 1,050 users the four workers are CPU-bound (about 390 % CPU = four full cores):
requests wait in line, page p95 climbs to 2.4 s, and still nothing fails. At the 1,600-user spike the server sheds
load on purpose instead of falling over: 246 requests got a `503` with `Retry-After`, 292 waits for a database
connection gave up after their timeout instead of piling up, and live connections were told to reconnect later
(WebSocket close `1013`). No request crashed, no worker restarted. Ten seconds after the load dropped back to 200
users, page p95 was under 1 s again and the database connections were back to normal within 4 s.

<details>
<summary><b>Production checks</b> - 12 passed, 1 not judged (memory growth needs the 30-minute soak test)</summary>

| Check | Target | Measured |
|:--|:--:|:--|
| ✅ Error rate at 200 users (abusers and junk excluded) | < 1 % | 0.00 % |
| ✅ Page response time p95 / p99 at 200 users | < 1 s / < 2.5 s | 22 ms / 77 ms |
| ✅ Server errors (5xx) at 200 users | 0 | 0 |
| ✅ Database statement / connection-pool timeouts at 200 users | 0 | 0 / 0 |
| ✅ Unhandled errors, whole run (HTTP 500, tracebacks, ASGI exceptions) | 0 | 0 |
| ✅ Worker restarts, whole run | 0 | 0 (4 workers started, none replaced) |
| ✅ One session per account | 0 sessions ended by another sign-in | 0 |
| ✅ Abusers throttled | every pattern refused | every pattern refused - see *Abuse and junk input* |
| ✅ Server errors under random junk input | 0 | 0 of 6,155 junk requests |
| ✅ Other users' data, open redirects, unescaped junk | 0 | 0 of 529 attempts to open other users' objects · 0 · 0 |
| ✅ AI questions of normal members answered | no "busy" or refusal | 0 of 133 (simulated provider) |
| ✅ Machine isolation | the SLO step not skewed | other programs ≤ 1.6 cores while the machine was ≤ 35 % busy |
| ➖ Memory growth | no leak | not judged: needs the 30-minute soak profile (`LT_PROFILE=soak`) |

</details>

<details>
<summary><b>Abuse and junk input</b> - every abuse pattern refused; 0 server errors from 6,155 junk requests; all 529 attempts to open other users' data refused</summary>

About 12 % of the simulated people misbehave on purpose, from address blocks no normal user shares:

| Abuse pattern | Requests | What the app did |
|:--|---:|:--|
| Password guessing on accounts that do not exist | 1,484 | the usual "wrong e-mail or password" for the first ~10 tries, then `429` and a `403` ban: 66 % refused |
| AI chat flood | 940 | `429` from the 7th question, then a `403` ban: 91 % refused - normal members' AI questions were never blocked |
| Identical search bursts | 7,478 | `429` after ~70 searches per address (half the bursts came from fresh addresses): 18 % refused |
| 404 directory scans | 1,035 | banned (`403`) from the 3rd probe: 93 % refused |
| Known scanner signatures | 227 | `403` from the first request: 100 % refused |
| WebSocket floods | 42 floods | 41 closed with `4429`; the 42nd timed out during the spike |

**Random junk input** (`JunkUser`, ~10 % of the users) sent 6,155 requests: every form with fields junked, emptied,
doubled or in the wrong encoding; NaN, Infinity, 5,000-digit and negative numbers; NUL bytes, broken UTF-8, 4-byte
emoji, right-to-left and zero-width text; malformed and 5,000-level nested JSON; fake and truncated images, an SVG
with a script, a PNG "decompression bomb"; odd methods and headers; junk WebSocket frames. The answers were 2,725 ×
2xx, 957 × 3xx and 2,380 × 4xx (validation `400` / `422`, `401`, `403`, `404`, `405`), **0 × 500**. The only 5xx
were 25 load-shedding `503`s during the spike and the seconds right after it. **All 529 attempts to open other users'
orders, chats, tasks, documents, staff and payroll records by id were refused**, no redirect left the site, and the
63,016 junk values seeded into the database (HTML, scripts, SQL, template strings) always came back escaped.

**AI.** 133 AI questions from normal members (assistant chat, plan questions, product questions) were all answered:
0 "busy", 0 refused. The AI provider was **simulated** (realistic latency, no model), so this measures the app's AI
guard and fair queue, not a model's speed.

</details>

<details>
<summary><b>How it was tested</b> - one laptop, production settings, 4 workers, simulated AI, 10 minutes</summary>

| | |
|:--|:--|
| 💻 **Machine** | one laptop: Apple silicon, 10 CPU cores, 16 GB RAM, macOS. The app, PostgreSQL 18, Redis and the load generator (Locust 2.47, 4 processes) all shared it |
| ⚙️ **App** | `ENVIRONMENT=production`, 4 uvicorn workers, database pool 10 + 5 per worker, shared Redis rate limits; its own database (`ironpulse_loadtest`), Redis db 9 and port 12000 - never the main database |
| 🗄️ **Data** | the data volume above, seeded fresh before the run (`loadtest/seed_loadtest.py`), with random junk in 10 % of the values of 27 text columns |
| 👥 **People** | 45 % anonymous visitors, 27 % members, 10 % junk senders, 9 % gym owners, 5 % staff, 2 % abusers, 3 super-admins; 2-8 s between clicks; every account used by one simulated person; live WebSocket on every page |
| 📶 **Shape** | 50 → 100 → **200 (SLO step)** → 350 → 700 → 1,050 → **1,600 (spike)** → 200 (recovery), 10 minutes |

</details>

<details>
<summary><b>Run history</b> - the two earlier runs failed on error tracebacks; both causes are fixed</summary>

| Run | Result | What it found | Fixed by |
|:--|:--:|:--|:--|
| `stress-20261113-121310` | ❌ | 100 tracebacks in the overload steps: database-pool timeouts in WebSocket session checks, background jobs and worker loops were logged as crashes | overload handling: one throttled warning instead of a traceback, `503` + `Retry-After`, WebSocket close `1013` with client back-off, jobs re-queued (`app/core/overload.py`) |
| `stress-20261113-132826` | ❌ | 5 tracebacks: the home page's featured-gyms query timed out during the spike | the home page renders without the featured gyms and logs one warning (`tests/test_home_overload.py`) |
| `stress-20261113-135009` | ✅ | every check passed | - |

</details>

**In the app:** the results are on **`/load-test`** - "Load test results" in everyone's navbar, signed in or not -
and in full (every endpoint, server resources, findings, downloads) for super-admins on **`/superadmin/load-test`**.
The raw results (`loadtest/results/`, hundreds of MB) are not committed, so a fresh clone shows an empty state until
you run the test yourself. **Run it yourself:** the exact commands are in
[loadtest/README.md](loadtest/README.md#4-the-10-minute-stress-run-exact-commands); `loadtest/readme_charts.py`
redraws the two charts above from any run's `summary.json`.

> [!IMPORTANT]
> These numbers come from **one run on one laptop**, with the app, the database, Redis and the load generator sharing
> 10 CPU cores, and a **simulated AI**. Real servers, networks, data and AI providers will behave differently. They are
> **not a performance guarantee, a capacity promise or a service level** - the software is provided "as is", without
> warranty of any kind; see [Disclaimer & no liability](#disclaimer--no-liability).
<!-- LOADTEST:END -->

## Highlights

| | |
|:--|:--|
| 🏋️ **Gym management** - gyms, AI-verified branches, up to 12 plan types per gym, staff, payroll and a shop dashboard | 🤖 **AI coach** - workout plans that respect your health conditions, a calendar, tasks and an assistant that acts on them |
| 🧠 **AI memory** - "What the AI knows about you": tell it once, edit it or make it forget | 🎙️ **Live voice coach** - an animated face with lip-sync, a Kokoro voice, voice commands and a safety stop |
| 🩺 **Health & safety** - implausible readings or red-flag symptoms pause gym activities until a gym admin reviews | ⚠️ **Trainer safety warnings** - a staff message is checked against the member's conditions before it is sent |
| 🛒 **Gym shop** - wishlist, cart, coupons, refunds, verified reviews, allergen warnings and AI product answers | 💬 **Live chat** - WebSocket chat with trainers and the gym team; notifications in-app and by e-mail |
| 🔁 **AI failover** - LAN gateway → local Ollama `gemma3:4b` → Claude → a friendly fallback, behind an AI guard and a fair queue | 🔐 **Secure by default** - strict CSP, CSRF, Argon2id, encryption at rest, IP bans; production refuses unsafe settings |
| 👤 **One session per account** - double-submit protection and a Google-style remembered-accounts chooser | ⚖️ **Trust & safety** - removal reasons, rehire rules, auto-ban, IP bans and a blocked-identity registry |
| 📏 **Caps on everything** - every list paged, every collection capped - see [Limits](#limits) | 📱 **LAN mode** - `./run.sh --lan` serves phones over HTTPS with a local, name-constrained CA |
| 🧪 **3,792 passing tests** - an N+1 guard on every GET route and 44 bug-hunt findings, all fixed | 🖥️ **Runs anywhere** - re-runnable setup for Windows, macOS and Linux, or everything in Docker |
| 📈 **Load tested on 2,086,466 database rows** - a 10-minute production-like stress test with random junk: 0 errors and 22 ms p95 at 200 users, within the SLO up to 700 users - see [Load test results](#load-test-results) | 🛡️ **Platform admin** - a separate super-admin sign-in (5 failures → IP ban), gym deletion and bans with payout failsafes, and an AI site overview |

## Contents

| 🚀 Get started | 🧭 How it works | 📚 Reference |
|:--|:--|:--|
| [Quick start](#quick-start) | [Architecture](#architecture) · [AI failover](#ai-provider-failover) | [Limits](#limits) |
| [macOS / Linux](#macos--linux) · [Windows](#windows-10--11) · [Docker only](#docker-only) | [Features by role](#features-by-role) | [Configuration (.env)](#configuration-env) · [OCR engines](#ocr-engines-per-operating-system) |
| [Use it on your phone (LAN)](#use-it-on-your-phone-lan) | [AI](#ai) · [What the AI knows about you](#what-the-ai-knows-about-you) | [Payments, maps, e-mail and sessions](#payments-maps-e-mail-and-sessions) |
| [Turn on the local AI fallback](#turn-on-the-local-ai-fallback) | [Live AI voice coach](#live-ai-voice-coach) | [The management CLI](#the-management-cli) · [Project structure](#project-structure) |
| [Demo data](#demo-data) · [Development workflow](#development-workflow) | [Health & safety](#health--safety) · [Trainer warnings](#trainer-safety-warnings-and-red-flags) | [Testing](#testing) · [Bug hunt](#bug-hunt-november-2026) |
| [Troubleshooting](#troubleshooting) | [Security & privacy](#security--privacy) · [Trust & safety](#staff-removal-and-bans-trust--safety) | [Deploying to production](#deploying-to-production) · [Load test results](#load-test-results) |
| [Accounts and sessions](#accounts-profiles-and-sessions) · [Platform admin](#platform-admin) | [Deletion and retention](#deletion-and-retention-policy) | [Disclaimer & no liability](#disclaimer--no-liability) · [License](#license) |

## Architecture

Built with FastAPI, async SQLAlchemy, PostgreSQL + pgvector, Redis, Jinja2, Tailwind CSS v4 and vanilla JavaScript.
The engineering contract - conventions, route map, business rules and cross-package APIs - is in
**[ARCHITECTURE.md](ARCHITECTURE.md)**. In short:

```mermaid
flowchart LR
    browser["Browser<br>server-rendered pages + vanilla JS"]
    subgraph server["FastAPI app - one or more uvicorn workers"]
        gate["Security gate<br>CSP · CSRF · rate limits · bans"]
        routers["Routers<br>public · member · staff · owner · super-admin"]
        services["Services<br>business rules · caps · audit"]
        aiq["AI guard + fair queue"]
        jobs["Job queue + periodic workers"]
        ws["WebSocket /ws<br>chat · notifications · AI status"]
        assets["Static files<br>Tailwind CSS + JS bundle"]
    end
    pg[("PostgreSQL + pgvector<br>source of truth")]
    redis[("Redis - optional<br>sessions · shared limits · presence")]
    ai["AI providers<br>gateway → Ollama → Claude"]
    ext["Kokoro TTS · OCR engines<br>Cashfree · SMTP · Geoapify"]

    browser -->|HTTPS| gate --> routers --> services
    browser -->|WebSocket| ws
    browser --> assets
    services --> pg
    services --> redis
    services --> aiq --> ai
    services --> ext
    jobs --> services
    ws -.->|"LISTEN bridge across workers"| pg
```

| Concern | Choice |
|---|---|
| Web | FastAPI + Starlette, server-rendered Jinja2 pages with progressive enhancement |
| Database | PostgreSQL 15+ with **pgvector**, `citext`, `pg_trgm` (the default setup installs 18, the Docker setup 17); async SQLAlchemy 2 (asyncpg); every relationship `lazy="raise"` (no N+1) |
| Sessions | Server-side sessions in PostgreSQL, cached in **Redis** when `REDIS_URL` is set |
| UI | Tailwind CSS v4 (standalone CLI, light/dark), one vanilla-JS bundle, strict CSP (no inline scripts) |
| AI | Gemma gateway (OpenAI SDK) -> Gemma in Docker (Ollama) -> Claude (Anthropic SDK) -> "AI unavailable", behind the AI guard and fair queue ([docs/ai-guard-and-queue.md](docs/ai-guard-and-queue.md)) |
| Voice | Kokoro TTS (OpenAI-compatible, Docker) with the browser's own voice as fallback |
| OCR | Several engines cross-validate ID cards (Tesseract, Apple Vision, Windows OCR, RapidOCR, Gemma/Claude vision) |
| Payments | Cashfree PG + Payouts; **TEST MODE** simulation without keys |
| Realtime | WebSocket `/ws` (chat, notifications, AI status); live events fan out to every worker through PostgreSQL |
| Background work | Durable job queue + periodic tasks in PostgreSQL (advisory locks) |

### AI provider failover

```mermaid
flowchart LR
    req["AI request<br>signed-in user"] --> guard{"AI guard<br>quota · spam · cooldown"}
    guard -->|refused| msg["Friendly message<br>not charged"]
    guard -->|admitted| queue["Fair queue<br>per-user caps"]
    queue --> gw["1 - Gemma gateway<br>LAN PC, OpenAI SDK"]
    gw -->|"down, slow or circuit open"| dk["2 - Gemma in Docker<br>Ollama gemma3:4b"]
    dk -->|"down, slow or circuit open"| cl["3 - Claude<br>Anthropic SDK, needs a key"]
    cl -->|"down or no key"| fb["4 - Feature fallback<br>rules or 'AI unavailable'"]
    gw & dk & cl --> ans["Answer<br>+ AI disclaimer"]
```

How each step decides, the timeouts and the circuit breaker: [AI providers and failsafes](#ai-providers-and-failsafes)
and [Local fallback LLM](#local-fallback-llm).

## Limits

Nothing a person or a gym creates can grow without bound, and no page reads more than one page of it: lists read one
page or window in SQL, counts stop at a cap and say "N+", aggregates read a bounded window, and a cap that is reached
answers in friendly words (never an error page). These are the shipped defaults. A **setting** is changed in `.env` (or
the environment); a **constant** is changed in the file named; the per-person and per-gym caps of `app/core/caps.py`
are tuned with the setting `CAPS_OVERRIDES` (e.g. `branches_per_gym=300`). `tests/test_limits_docs.py` checks every
row against the code.

107 limits in 13 groups - open one to see its table:

<details>
<summary>Accounts and sessions - 4 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Accounts** · notifications (bell, `/account/notifications`) | 20 per window ("Show older"); the JSON feed at most 50; unread / total counts stop at **999+** | `PAGE_SIZE`, `FEED_MAX`, `COUNT_CAP` in `app/routers/account.py` |
| **Accounts** · sessions | one live session per account - a new sign-in ends the others ("signed in on another device") | setting `SINGLE_SESSION_PER_ACCOUNT` (`true`) |
| **Accounts** · remembered accounts (sign-in page) | 5 per device, 10 devices per account, gone after 180 days unused; a look-up takes at most 5, 30 look-ups + removals a minute per network address; photo links expire after 10 minutes | `MAX_PER_DEVICE`, `MAX_PER_USER`, `UNUSED_DAYS`, `LOOKUP_MAX_REFS`, `LOOKUPS_PER_MINUTE`, `PHOTO_URL_SECONDS` in `app/services/remembered_accounts.py` |
| **Accounts** · double-submit protection | an outcome is replayed to duplicates for 10 minutes; a duplicate waits at most 25 s for the first request; replayed bodies up to 16 KB (JSON / redirects) and 256 KB (pages); without Redis each worker keeps at most 20,000 outcomes / 32 MB | `WINDOW_SECONDS`, `WAIT_SECONDS`, `MAX_JSON_BYTES`, `MAX_HTML_BYTES`, `LOCAL_MAX_ENTRIES`, `LOCAL_MAX_BYTES` in `app/core/submit_guard.py` |

</details>

<details>
<summary>Health, coach and AI memory - 9 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Health** · conditions, allergies, medications and injuries | 50 per member (the form says so) - in total, whoever adds them: the member's form, "Remember this" in the AI chat, the gym's registration and health edit (race-safe, one lock per member); a gym's registration / AI intake 20 per list × 4 lists; the gym's health edit adds at most 20 entries to a list per save (12,000 characters per list; the form's snapshot of the entries it showed is valid 24 h and names at most 200) - the 50 in total still hold | `MAX_CONDITIONS` in `app/routers/customer_health.py` and `app/services/health.py`; `MAX_ITEMS`, `EDIT_TEXT_MAX`, `SNAPSHOT_MAX_AGE`, `SNAPSHOT_ROWS_MAX` in `app/services/intake.py` |
| **Health** · entries read for one member (pages, AI prompts, live coach, jobs) | 100, safety-relevant first (one row of every safety tag before the rest) | `CONDITIONS_READ_MAX` in `app/services/health.py` |
| **Health** · history and alerts | 30 readings per page; alerts 20 per page, the latest 6 on `/me/health` | `HISTORY_PER_PAGE`, `ALERTS_PER_PAGE`, `ALERTS_SHOWN` in `app/routers/customer_health.py` |
| **Health** · new alerts and AI safety reviews | 20 new health alerts per member per day - one alert per reading and source (the rules, the AI), updated in place and shown unread again when the day's reading is logged again; past it the newest alert of the same kind is updated, never dropped. 10 AI safety reviews of abnormal health readings per member per day - past them the safety rules decide alone and the note says so; nothing is refused | caps `health_alerts_per_member_day`, `health_reviews_per_member_day` in `app/core/caps.py` (setting `CAPS_OVERRIDES`; fallbacks `ALERTS_PER_DAY`, `REVIEWS_PER_DAY` in `app/services/health.py`) |
| **Health** · what the AI remembers (training preferences) | 20 exercise types enjoyed and 20 avoided, 10 goals, 30 equipment items; own labels 40 characters (goals 60); notes 500 characters; 1-14 sessions a week, 10-240 minutes; 50 forget requests remembered | `LIST_CAPS`, `ITEM_MAX`, `NOTES_MAX`, `SESSIONS_RANGE`, `MINUTES_RANGE`, `FORGOTTEN_MAX` in `app/services/fitness_profile.py` |
| **AI** · memory offers | 5 "Remember this?" offers per message, 10 items per forget, tickets valid 24 h | `MAX_PROPOSALS`, `FORGET_MAX`, `TOKEN_MAX_AGE` in `app/ai/memory.py` |
| **AI** · saved health text in prompts | each saved text is checked whole for instruction-like wording (at most 4,000 characters read) before it is quoted | `INJECTION_SCAN_MAX` in `app/ai/member_context.py` |
| **Live coach** · workouts | 30 started per 24 h; 2,000 events logged per workout (safety events always) | `SESSIONS_PER_DAY`, `SESSION_EVENTS_MAX` in `app/services/live_coach.py` |
| **Coach** · plans | 20 generated per 24 h | `PLANS_PER_DAY` in `app/services/coach.py` |

</details>

<details>
<summary>Tasks and chat - 10 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Tasks** · open tasks and reminders per person | 500 - the form answers "You've reached the task limit", the assistant says so in words, reopening a done task counts | setting `TASKS_MAX_OPEN` (fallback `MAX_OPEN_TASKS` in `app/services/tasks.py`) |
| **Tasks** · one calendar day | 15 tasks in the month grid and the day panel, then "+N more" → `/tasks?day=` | `CALENDAR_DAY_MAX` in `app/services/tasks.py` |
| **Tasks** · task lists | 50 per page on `/tasks` and `/tasks?day=` (`?per_page=` at most 100); one date range (e.g. the assistant's "this week") reads at most 1,000 | `TASKS_PER_PAGE`, `TASKS_MAX_PER_PAGE`, `RANGE_TASKS_MAX` in `app/services/tasks.py` |
| **Chat** · conversations | 50 per window ("Load more conversations") | `THREAD_PAGE_SIZE` in `app/services/chat.py` |
| **Chat** · messages | 50 per window; one message at most 4,000 characters | `PAGE_SIZE`, `MAX_BODY` in `app/services/chat.py` |
| **Chat** · misconduct reports | 30 per page (a member's and an owner's lists) | `REPORTS_PER_PAGE`, `OWNER_REPORT_PAGE` in `app/routers/chat.py` |
| **Chat** · new conversations and messages per member | 20 new conversations and 500 messages in any 24 hours (race-safe; a friendly "try again tomorrow"); a message that mentions red-flag symptoms is never refused | `CHAT_CAPS` (`chat_threads_per_member_day`, `chat_messages_per_member_day`) in `app/services/chat.py` - `CAPS_OVERRIDES` tunes them |
| **Chat** · messages from the gym team | 2,000 chat messages per staff member or gym owner and 300 gym-team messages per conversation in any 24 hours (race-safe; a friendly "try again tomorrow, or phone the member"); a member's red-flag message is never refused | `CHAT_CAPS` (`chat_messages_per_staff_day`, `chat_staff_messages_per_conversation_day`) in `app/services/chat.py` - `CAPS_OVERRIDES` tunes them |
| **Chat** · misconduct reports per member | 10 reports waiting for a reply and 10 new reports in any 24 hours; one open report per staff member (race-safe: parallel submits file one; a unique index is the last line of defence) | `REPORT_CAPS` (`open_reports_per_member`, `reports_per_member_day`) in `app/routers/chat.py` - `CAPS_OVERRIDES` tunes them |
| **Chat** · trainer safety panel, warnings and red-flag alerts | the panel reads 50 health entries (safety-relevant first) and lists 12 per list and 12 library exercises; a warning names at most 5 flagged exercises; one red-flag alert per conversation per hour (the latest 200 messages are checked); list badges for at most 100 members per window | `PANEL_READ_MAX`, `PANEL_LIST_MAX`, `PANEL_EXERCISES_MAX`, `WARNING_FLAGS_MAX`, `RED_FLAG_WINDOW`, `RED_FLAG_SCAN`, `BADGE_IDS_MAX` in `app/services/trainer_safety.py` |

</details>

<details>
<summary>Gym shop - 9 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Shop** · bag | 50 different products, 30 of them from one store (one order) | `MAX_CART_LINES`, `MAX_STORE_LINES` in `app/services/shop.py` |
| **Shop** · wishlist | 500 products, 24 per page | `WISHLIST_MAX`, `WISHLIST_PAGE_SIZE` in `app/services/shop.py` |
| **Shop** · catalogue | 24 per page and at most 500 pages; the product count stops at **12,000+** | `PAGE_SIZE`, `CATALOGUE_MAX_PAGE` (the count cap is both multiplied) in `app/services/shop.py` |
| **Shop** · store filter and counts | the 20 stores with the most products, plus a store search of 20 results; category counts, top stores and store searches are cached 60 s per process | `STORE_OPTIONS`, `STORE_SEARCH_LIMIT`, `FACETS_TTL` in `app/services/shop.py` |
| **Shop** · reviews on a product page | 20 at a time ("Older reviews") | `REVIEWS_PAGE_SIZE` in `app/services/shop.py` |
| **Shop** · my orders | 20 per page; an order's timeline shows its latest 50 events | `ORDERS_PAGE_SIZE` in `app/services/shop.py`; `ORDER_EVENTS_LIMIT` in `app/routers/shop.py` |
| **Shop** · safety panel and AI product answers | the panel reads 50 of the member's health entries (one per tag, allergies and medications first); the AI prompt names at most 10 by the member's own words (60 characters each), those behind the product's safety notes first and untagged notes last, and counts the rest ("+N more saved, not shown"); at most 3 "Check the ingredients" warnings per product | `MAX_CONDITIONS`, `PROMPT_CONDITIONS`, `MAX_ALLERGY_MENTIONS` in `app/services/shop.py`; `ITEM_PROMPT_MAX` in `app/ai/shop.py` |
| **Shop** · stock held at checkout | an unpaid order holds its units for 15 minutes: other checkouts can't take held units (told before paying); "Pay again" takes a lapsed hold again only while the units are free; a late payment that would take another buyer's held units is refunded automatically | `STOCK_HOLD_MINUTES` in `app/services/shop.py` |
| **Shop** · product stars and "Top rated" | rewritten in the same transaction as every review change (an account purge too); an hourly reconcile run is the safety net: 200 products per transaction, 50 s per run | `RATINGS_EVERY_S`, `RATINGS_BATCH`, `RATINGS_BUDGET_S` in `app/services/shop.py` |

</details>

<details>
<summary>Public pages and jobs - 5 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Public** · home page | featured gyms and the trainer teaser cached 120 s, the site figures 300 s (per process) | `HOME_TTL`, `STATS_TTL` in `app/routers/public.py` |
| **Public** · gym directory `/gyms` | 12 per page; "near me" lists gyms with a published branch within 100 km first, nearest first (the rest A-Z); the city filter offers the 60 busiest cities (cached 300 s) | `PAGE_SIZE`, `NEAR_SORT_KM`, `DIRECTORY_CITIES`, `CITIES_TTL` in `app/routers/public.py` |
| **Public** · gym page | 12 branch cards + "View all N locations" (`/gyms/{slug}/branches`, 12 per page) | `BRANCH_PAGE_SIZE` in `app/routers/public.py` |
| **Public** · jobs board | 10 per page; "Near me" radius 5 / 10 / 20 / 30 km and distances shown only up to 30 km; a posting whose branch is unpublished is left out of "Near me" (still in "All jobs") | `JOBS_PAGE_SIZE`, `JOB_RADII_KM` in `app/routers/public.py`; the 30 km is setting `OFFLINE_MAX_DISTANCE_KM` |
| **Public** · job enquiry (anonymous form) | 20-4,000 characters with at most 2 links (none in the name); sent at least 3 s and at most 2 days after the page was served (else "send again", details kept); one enquiry per network address and posting per 5 min (429); the same message to the same posting refused for 24 h (409) - case, spacing, invisible characters and Unicode compatibility forms aside; a hidden honeypot field | `ENQUIRY_MIN_CHARS`, `ENQUIRY_MAX_CHARS`, `ENQUIRY_MAX_LINKS`, `ENQUIRY_MIN_FILL_SECONDS`, `ENQUIRY_FORM_MAX_AGE`, `ENQUIRY_COOLDOWN_SECONDS`, `ENQUIRY_DUPLICATE_HOURS` in `app/routers/public.py` |

</details>

<details>
<summary>Gym owners and staff: members, staff, trust & safety - 15 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Owner** · members list | 24 per page; 3 health chips per member (+N) | `PAGE_SIZE`, `HEALTH_LABELS_SHOWN` in `app/routers/owner_customers.py` |
| **Owner** · which members a gym sees | members with an active or suspended membership, a purchase they started in their own app, or a membership that expired within the last 90 days - and members the owner registered; a cancelled or failed membership, a desk purchase that never became a membership or one that ended earlier gives no access to the member's profile or health record (404) | `FORMER_MEMBER_DAYS` in `app/services/intake.py` |
| **Owner** · a member's profile | memberships 12 per page; health entries 50 per kind and 100 in all, safety-relevant first | `SUBS_PAGE_SIZE`, `CONDITIONS_PER_KIND`, `CONDITIONS_SHOWN` in `app/routers/owner_customers.py` |
| **Owners** · a member's conversations on their profile | the 20 with the newest activity | `THREADS_SHOWN` in `app/routers/owner_customers.py` |
| **Owner** · member invite e-mails (`POST /owner/customers/{id}/invite`) | one per member per 2 min and at most 5 in any 24 h (the registration's own invite included); parallel re-sends are serialised per member, so only one link is ever live | `INVITE_RESEND_INTERVAL`, `INVITES_PER_MEMBER_DAY` in `app/routers/owner_customers.py` |
| **Owner** · audit log `/owner/audit` | 40 per page; the count stops at **10,000+** | `AUDIT_PAGE_SIZE`, `AUDIT_COUNT_CAP` in `app/routers/owner_customers.py` |
| **Owner** · staff list and a trainer's clients | 30 per page | `STAFF_PER_PAGE`, `CLIENTS_PER_PAGE` in `app/routers/owner_staff.py` |
| **Staff** · a trainer's clients and client page | 30 clients per page; health entries 25 per kind and 100 in all, safety-relevant first | `CLIENTS_PER_PAGE`, `CONDITIONS_PER_KIND`, `CONDITIONS_SHOWN` in `app/routers/staff_portal.py` |
| **Owner** · staff activity | 50 events per window ("Show older activity") | `TIMELINE_LIMIT` in `app/routers/owner_staff.py` |
| **Owner** · staff invitation re-sends (`POST /owner/staff/{id}/resend-invite`) | one per staff record per 120 s and at most 5 in any 24 h (the first invitation not counted); parallel re-sends are serialised per record | `INVITE_RESEND_SECONDS`, `STAFF_INVITE_RESENDS_PER_DAY` in `app/routers/owner_staff.py` |
| **Owner** · job postings and enquiries | 20 and 25 per page | `JOBS_PER_PAGE`, `ENQUIRIES_PER_PAGE` in `app/routers/owner_staff.py` |
| **Owner** · "Assign a client" picker | 50 members at once (a name search narrows it down) | `ELIGIBLE_LIMIT` in `app/services/staffing.py` |
| **Owner** · trainer capacity (automatic assignment) | 150 active clients per trainer; when every match is full the member joins unassigned and the owner is told at most once per gym per 24 h | setting `TRAINER_MAX_CLIENTS` (fallback constant in `app/services/checkout.py`); `TRAINER_NOTICE_HOURS` in `app/services/checkout.py` |
| **Owner** · certificate re-check (`POST /owner/staff/{id}/recheck`) | once per 10 min per staff record, a certificate at most 3 times, 3 certificates per request (newest first), one AI `vision` unit each | `REREAD_COOLDOWN_SECONDS`, `REREAD_LIMIT`, `REREAD_BATCH` in `app/services/ocr.py` |
| **Trust & safety** · ending employments and bans | a removal for a conduct or severe reason needs a 10-500 character note (500 for any note); a ban is lifted with a 5-500 character note; conduct violations reported by 2 different gym owners within 365 days ban the account; a severe violation bans at most 5 of the account's public addresses seen in the last 30 days, for 30 days, never one that more than 3 other active accounts used; a violation also withdraws at most 50 of the person's other pending records at the same owner's gyms | `NOTE_MIN`, `NOTE_MAX`, `LIFT_NOTE_MIN`, `LIFT_NOTE_MAX`, `MAX_BANNED_IPS`, `IP_LOOKBACK_DAYS`, `WITHDRAW_PENDING_MAX` in `app/services/trust_safety.py`; settings `VIOLATION_AUTOBAN_THRESHOLD`, `VIOLATION_WINDOW_DAYS`, `SEVERE_IP_BAN_DAYS`, `SHARED_IP_THRESHOLD` |

</details>

<details>
<summary>Gym owners: finance, payroll and shop admin - 9 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Owner** · transactions page | the last 12 months by default (30 days, 90 days, 12 months, All time, or From/To dates); 25 per page | `DEFAULT_PERIOD`, `PERIODS`, `PAGE_SIZE` in `app/routers/owner_finance.py` |
| **Owner** · transaction totals | counted and summed over at most 50,000 transactions per view - above that "50,000+", the sums are hidden and paging stops (pick a shorter period); `?page=` at most 10,000 | `TOTALS_ROW_CAP`, `MAX_TX_PAGE` in `app/routers/owner_finance.py` |
| **Owner** · CSV export | follows the page's period, streamed in batches of 2,000 rows; above 100,000 rows it asks for a narrower date range | `EXPORT_BATCH`, `EXPORT_MAX_ROWS` in `app/routers/owner_finance.py` |
| **Owner** · finance overview | the latest 8 transactions and 6 unpaid statements | `RECENT_TX_LIMIT`, `PENDING_STATEMENT_LIMIT` in `app/routers/owner_finance.py` |
| **Owner** · payroll | statements 30 per page; the staff filter offers 300 people (current staff first) | `STATEMENT_PAGE_SIZE`, `EMPLOYEE_OPTIONS_LIMIT` in `app/routers/owner_finance.py` |
| **Owner** · "Run payroll" | for all gyms (more than one) a background job with live progress, up to 3 attempts, one gym per transaction; at most 50 AI explanations queued per run (the rest on "Explain"); a single gym is generated at once | `GENERATE_ATTEMPTS`, `EXPLAIN_PER_RUN` in `app/services/payroll.py` |
| **Owner** · payroll progress card | polls every 4 s for the first 10 polls, then every 10 s, and stops after 120 (about 19 min; live updates and the end-of-run notice still arrive) | component `payroll-run` in `app/static/src/js/31-finance.js` |
| **Owner** · shop admin | orders 30, products 50, coupons 30, reviews 30 per page; an order's timeline shows its latest 50 events | `ORDERS_PER_PAGE`, `PRODUCTS_PER_PAGE`, `COUPONS_PER_PAGE`, `REVIEWS_PER_PAGE`, `ORDER_EVENTS_LIMIT` in `app/routers/owner_shop.py` |
| **Owner** · low-stock digest | at most one per owner in 20 h, naming 6 products (+N more) | `LOW_STOCK_REPEAT_HOURS`, `LOW_STOCK_LISTED` in `app/services/shop.py` |

</details>

<details>
<summary>Super-admin - 8 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Super-admin** · security centre | the last 7 days by default; 40 events per page; counts stop at **10,000+**; facets aggregate the newest 50,000 matching events | `DEFAULT_EVENT_WINDOW`, `EVENTS_PER_PAGE`, `COUNT_CAP`, `SCAN_CAP` in `app/routers/superadmin.py` |
| **Super-admin** · other lists | users 25, bans 25, outbox 30, audit 40 per page; review queues 50 at a time; the audit area filter reads 500 distinct actions | `USERS_PER_PAGE`, `BANS_PER_PAGE`, `OUTBOX_PER_PAGE`, `AUDIT_PER_PAGE`, `QUEUE_LIMIT`, `AUDIT_MAX_ACTIONS` in `app/routers/superadmin.py` |
| **Super-admin** · trust & safety `/superadmin/trust` | 25 per page in each of its four lists (account bans, conduct violations, network addresses, blocked identities); counts stop at **10,000+** | `PER_PAGE` in `app/routers/superadmin_trust.py`; `COUNT_CAP` in `app/routers/superadmin.py` |
| **Super-admin** · AI page `/superadmin/ai` | providers, features and the top 10 users over the last 24 h; recent AI blocks over 7 days (20 shown); the demo-account count cached 600 s per process | `AI_WINDOW`, `TOP_AI_USERS`, `AI_BLOCKS_WINDOW`, `AI_BLOCKS_SHOWN`, `AI_DEMO_COUNT_TTL` in `app/routers/superadmin.py` |
| **Super-admin** · security triage | unreviewed events older than 48 h are closed as "expired", never triaged; "events left" stops counting at 10,000 | `STALE_AFTER`, `PENDING_COUNT_CAP` in `app/services/security_triage.py` |
| **Super-admin** · platform admin sign-in `/superadmin/login` | **5** failed attempts from one address within **15 min** ban it for **24 h** (a second ban within 30 days: permanent); loopback is counted, never banned; the account's own sign-in lock applies too | `SUPERADMIN_LOGIN_MAX_FAILURES`, `SUPERADMIN_LOGIN_WINDOW_MINUTES`, `SUPERADMIN_LOGIN_BAN_HOURS`, `SUPERADMIN_ALLOWED_IPS` |
| **Super-admin** · governance `/superadmin/governance` | **30** destructive actions per admin per hour; a gym's deletion cooling-off **7 days**; one ban ends at most 50 employments or suspends at most 100 gyms; 50 final settlements per click; refunds and order settlements 100 per background run; 300 staff rows on a settlement desk | `GOVERNANCE_ACTIONS_PER_HOUR`, `GYM_DELETE_COOLING_OFF_DAYS`; `EMPLOYMENTS_MAX`, `OWNER_GYMS_MAX`, `SETTLEMENTS_PER_CLICK`, `JOB_BATCH`, `DESK_STAFF_MAX` in `app/services/governance.py` |
| **Super-admin** · site overview `/superadmin/insights` | **10** AI questions a minute per admin, 2-500 characters each (the `admin_overview` AI quota also allows 15 a minute, 300 an hour, 1,500 a day); the digest is cached **120 s**; lists show the top 8, at most 60 people are named, aggregates read the newest 50,000 rows; refusal counters keep 2,000 ranked subjects a day for 8 days | `ASK_PER_MINUTE`, `QUESTION_MIN`, `QUESTION_MAX` in `app/routers/superadmin_insights.py`; `DIGEST_TTL`, `TOP_N`, `PEOPLE_MAX`, `SCAN_CAP`, `WHO_CAP` in `app/services/admin_insights.py`; `admin_overview` in `app/ai/policy.py` |

</details>

<details>
<summary>AI quotas, queue and providers - 11 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **AI** · per-user quotas | per minute / hour / day by account tier and group - e.g. a member with an AI plan: chat 12 / 150 / 500, plan and product questions 8 / 60 / 200, plans and intake 3 / 12 / 30, photo checks 4 / 20 / 40, coach voice 90 / 1,500 / 5,000; a member without an AI plan gets fewer; only signed-in users get AI | `QUOTAS` in `app/ai/policy.py`; setting `AI_QUOTA_SCALE` multiplies every quota |
| **AI** · cooldown ladder | refusals earn strike points over 15 min (spam 3, flood 2 - 5 refusals within 10 s -, the rest 1; the first 2 duplicates and the first 5 abandoned answers are free): 6 points pause AI for 2 min, 12 for 15 min, 20 for 60 min; the 3rd pause in a day lasts 24 h; pauses of 60 min or more are reported to the security centre | `STRIKE_WINDOW`, `STRIKE_POINTS`, `DUPLICATE_FREE`, `ABORT_FREE`, `FLOOD_BURST`, `COOLDOWN_LADDER`, `REPEAT_COOLDOWN_SECONDS` in `app/ai/policy.py` |
| **AI** · what is charged | an answer counts once the AI started it (a stream after its first words, a completion once a provider slot was held), a coach voice line once its synthesis started; refused, busy, queued-then-stopped and failed calls are free | `app/ai/client.py` (`ai.complete` / `ai.stream`), `app/services/tts.py` |
| **AI** · fair queue | 4 queued + running calls per person (2 running), 200 in the whole queue, 4 language-model calls and 2 voice syntheses at once (per process); beyond that "AI is busy" | settings `AI_QUEUE_MAX_PER_USER`, `AI_QUEUE_MAX_TOTAL`, `AI_QUEUE_CONCURRENCY`, `AI_TTS_CONCURRENCY`; `MAX_RUNNING_PER_USER` in `app/ai/policy.py` |
| **AI** · text and voice | 4,000 characters per guarded request; 400 characters per voice line | `MAX_TEXT_CHARS` in `app/ai/policy.py`; `MAX_TEXT` in `app/services/tts.py` |
| **AI** · leaving | AI work for someone who closed every page stops 45 s later (pausable jobs wait for them) | setting `PRESENCE_GRACE_SECONDS` |
| **AI** · assistant history | the newest 1,000 messages per person (older ones trimmed) | `ASSISTANT_HISTORY_MAX` in `app/ai/assistant.py` |
| **AI** · health note of a normal reading | AI-written only for a membership whose plan includes AI; everyone else gets the deterministic note, with no AI call | `app/services/health.py` (`NOTE_FEATURE`) |
| **AI** · failover between providers | a provider gets 2 s to accept the connection (Claude 5 s), then the next one is asked; 2 failures in a row skip it for 45 s | `CONNECT_TIMEOUT_SECONDS`, `CLOUD_CONNECT_TIMEOUT_SECONDS` in `app/ai/client.py`; settings `AI_CIRCUIT_FAILURES`, `AI_CIRCUIT_COOLDOWN_SECONDS` |
| **AI** · local fallback (Ollama in Docker) | one answer at a time; 6 GB of memory (4 GB reserved); its logs 3 × 10 MB; a streamed answer gets at least 25 s for its first words (a laptop CPU needs 5-15 s), never beyond the feature's whole-answer budget | setting `AI_DOCKER_MAX_CONCURRENCY`; `OLLAMA_MEMORY_LIMIT`, `OLLAMA_MEMORY_RESERVATION` (`.env`) in `docker/compose.yml`; setting `AI_DOCKER_FIRST_TOKEN_SECONDS` |
| **AI** · simulated provider's bug-hunt aids | the prompt capture file stops growing at 256 MB; at most 20 nonces echoed per answer | `CAPTURE_MAX_MB` (`AI_SIM_CAPTURE_MAX_MB`), `MAX_NONCES` in `app/ai/simulated.py` |

</details>

<details>
<summary>Network and uploads - 8 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Network** · per network address (one allowance for all workers with Redis) | 240 requests/min in all; sign-in 10/min; desk check-in and OTP 5 per 10 min; AI 60/min; live workout 120/min; uploads 30/min; static files 1,500/min - then 429 with `Retry-After` | settings `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_AUTH_PER_MINUTE`, `RATE_LIMIT_AI_PER_MINUTE`, `RATE_LIMIT_LIVE_PER_MINUTE`, `RATE_LIMIT_UPLOAD_PER_MINUTE`, `RATE_LIMIT_STATIC_PER_MINUTE`; the OTP bucket in `app/security/ratelimit.py` |
| **Network** · live connection (`/ws`) | 16 open sockets per person (the next is refused), 40 messages per 10 s per socket, 4,096 characters per message | `WS_MAX_SOCKETS_PER_USER`, `WS_FLOOD_MESSAGES`, `WS_FLOOD_WINDOW`, `WS_MAX_MESSAGE_CHARS` in `app/routers/core.py` |
| **Network** · IPv6 clients | rate limits, threat scores and bans key an internet IPv6 client by its /64 (one household or phone); LAN, link-local and unique-local IPv6 addresses (phones in LAN mode) by their own address; 429s of the global and static buckets never add threat points (a busy gym NAT is throttled, never banned) | `IPV6_PREFIX` in `app/security/ratelimit.py` |
| **Network** · shared limits during a slow Redis | at most 32 Redis calls in flight per worker; a request waits at most 1 s for a slot, then is judged from the worker's memory (which mirrors the shared state); a Redis call slower than 250 ms falls back the same way; after failures Redis is asked again after 2 s (±25 %, one probe at a time) | `MAX_CONNECTIONS`, `QUEUE_WAIT_SECONDS`, `RETRY_SECONDS`, `RETRY_JITTER` in `app/security/ratelimit.py`; setting `RATE_LIMIT_REDIS_TIMEOUT_MS` |
| **Network** · static files | a `?v=<content version>` URL is cached for 1 year (`immutable`); any other static URL is revalidated on every use (ETag) | `IMMUTABLE`, `REVALIDATE` in `app/core/static.py` |
| **Network** · ids in addresses and forms | a number outside 1 … 2⁶³-1 (the database's 64-bit ids) is answered 422 on the owner and staff routes and 404 everywhere else - never a server error | `IdParam` (`MAX_ID`) in the owner / staff routers; `id_out_of_range` in `app/core/errors.py` |
| **Uploads** · images and PDFs | a file at most 8 MB; an image at most 25 megapixels as decoded, checked on its header before any pixel is decoded (a single-scan JPEG photo up to 64 megapixels: it is decoded at 1/2-1/8 scale) - "That image is too large"; 2 image decodes at once per worker process, the next upload waits at most 30 s, then "busy - please try again"; a PDF 1-30 pages | setting `MAX_UPLOAD_MB`; `MAX_IMAGE_PIXELS`, `MAX_JPEG_PIXELS`, `IMAGE_DECODE_SLOTS`, `IMAGE_DECODE_WAIT_SECONDS`, `PDF_MAX_PAGES` in `app/services/storage.py` |
| **Network** · LAN mode (`./run.sh --lan`) certificates | the server certificate is valid 397 days and renewed 30 days before it expires; the local CA 3650 days | `LEAF_DAYS`, `RENEW_DAYS`, `CA_DAYS` in `scripts/lan.py` |

</details>

<details>
<summary>Database and background work - 4 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Database** · statement time | a request's statement is stopped after 15 s: friendly 503 "taking too long - please try again" | setting `DB_STATEMENT_TIMEOUT_MS` |
| **Database** · idle transaction | 60 s in a request, 10 min for background work | settings `DB_IDLE_TX_TIMEOUT_MS`, `DB_BACKGROUND_IDLE_TX_TIMEOUT_MS` |
| **Database** · connections per worker process | 10 + 5 overflow (+1 LISTEN with several workers); a request waits up to 10 s for one, then the friendly 503; each worker warns at startup when all workers could claim more than 80 % of PostgreSQL's slots | settings `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S`; `BUDGET_SHARE` in `app/db.py` |
| **Background** · e-mail and security flushes | a claimed e-mail batch keeps a 15 min lease (a batch whose worker died is claimed again: delivery at least once); every 2 s at most 1,000 security events, 200 ban decisions and 500 AI call logs are written, each kind in its own transaction - a batch that is not committed goes back to its queue | `CLAIM_LEASE` in `app/services/mailer.py`; `FLUSH_EVENTS_MAX`, `MAX_BANS_PER_FLUSH` in `app/security/bans.py`; `CALL_LOG_FLUSH_MAX` in `app/ai/client.py` |

</details>

<details>
<summary>Retention - 9 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Retention** · security events | 90 days | setting `RETENTION_SECURITY_DAYS` (`0` keeps them) |
| **Retention** · audit log | 365 days | setting `RETENTION_AUDIT_DAYS` |
| **Retention** · AI call log | 30 days | setting `RETENTION_AI_LOG_DAYS` |
| **Retention** · e-mail already sent, logged or failed | 30 days | setting `RETENTION_EMAIL_DAYS` |
| **Retention** · finished background jobs | 14 days | setting `RETENTION_JOBS_DAYS` |
| **Retention** · read notifications | 90 days (unread ones are kept - at most the newest 1,000 per person, see **Caps** · notifications) | setting `RETENTION_NOTIFICATION_DAYS` |
| **Retention** · lifted or expired IP bans | 90 days (never an active or permanent ban; never less than the 30-day repeat-offence window) | setting `RETENTION_IP_BAN_DAYS` |
| **Retention** · used, superseded or expired one-time links | 30 days (an unused invite is kept) | setting `RETENTION_AUTH_TOKEN_DAYS` |
| **Retention** · expired sign-in sessions | 30 days (live sessions are never touched) | setting `RETENTION_SESSION_DAYS` |

</details>

<details>
<summary>Caps per person and per gym - app/core/caps.py - 9 limits</summary>

| Limit | Value | Where to change it |
|---|---|---|
| **Caps** · gyms per person | 3 gyms with an online or combo membership per person and 1 gym with an in-person (gym or combo) membership per person - distinct gyms over active, suspended and unfinished memberships (an unfinished purchase is discarded after 7 days, or at once under "My memberships"); a combo plan uses both; renewing or switching plans at a gym already held never counts, moving up from online to gym / combo there needs the in-person slot; checked under a per-person lock on every join (member checkout, the owner's desk registration), again before payment and at activation (a payment that still slipped through is never activated: it is held for the gym to refund and both sides are told); members already above a limit keep every membership | settings `MAX_ONLINE_GYMS`, `MAX_OFFLINE_GYMS` (caps `online_gyms`, `offline_gyms` in `app/core/caps.py`) |
| **Caps** · gyms and branches | 20 gyms per owner account; 200 branches (open or closed) per gym; 12 photos per branch; 20 documents per branch | caps `gyms_per_owner`, `branches_per_gym`, `branch_photos`, `branch_documents` in `app/core/caps.py` (setting `CAPS_OVERRIDES`, e.g. `branches_per_gym=300`) |
| **Caps** · staff and jobs | 500 current staff records (invited, in verification, active or suspended) per gym; 50 unanswered staff invitations per gym; 40 documents per staff member; 50 weekly shifts per staff member; 50 open job postings per gym; 20 new job postings per gym per day; 200 enquiries per job posting per day (then 429) | caps `staff_per_gym`, `staff_invites_per_gym`, `employee_documents`, `shifts_per_employee`, `open_jobs_per_gym`, `jobs_per_gym_day`, `enquiries_per_job_day` in `app/core/caps.py` (setting `CAPS_OVERRIDES`) |
| **Caps** · job enquiries are for job seekers | a signed-in gym owner, super-admin or staff member with a current employment (active or suspended, at any gym) sees why instead of the enquiry form, and the enquiry route refuses them before any spam accounting (403 page, or JSON `{"code": "job_seekers_only"}`); guests, members and former staff enquire as before | `seeker_block` in `app/routers/public.py` |
| **Caps** · members, tasks and ID cards | 200 members registered at the desk per owner per day; 300 new tasks and reminders per person per day (open ones: **Tasks** above); 30 ID card uploads per account (at most 5 a day) | caps `members_added_per_owner_day`, `tasks_per_person_day`, `id_documents_per_user` in `app/core/caps.py` (setting `CAPS_OVERRIDES`); `MAX_UPLOADS_PER_DAY` in `app/routers/identity.py` |
| **Caps** · gym shop | 2,000 products per gym shop; 8 photos per product; 200 coupons per gym shop; 200 manual stock changes per product per day; 10 unpaid orders per member; 30 orders per member per day; a coupon with a use limit also counts the unpaid orders carrying it, so buyers at the same moment never redeem it more often than allowed; the bag and the wishlist hold under parallel adds | caps `products_per_gym`, `product_images`, `coupons_per_gym`, `stock_changes_per_product_day`, `open_orders_per_member`, `orders_per_member_day` in `app/core/caps.py` (setting `CAPS_OVERRIDES`); `reserve_coupon` in `app/services/shop.py` |
| **Caps** · wellness check-ins | 10 wellness check-ins per member per day (race-safe; checked before any AI time is spent); a check-in that reports a red-flag symptom is never refused | cap `wellness_checks_per_member_day` in `app/core/caps.py` (setting `CAPS_OVERRIDES`) |
| **Caps** · notifications | 1,000 notifications kept (the newest, read or not; the nightly clean-up removes older ones) per person | cap `notifications_kept` in `app/core/caps.py` (setting `CAPS_OVERRIDES`); `TRIM_USERS`, `TRIM_ROWS` in `app/services/retention.py` |
| **Caps** · storage quotas | 2,048 MB of uploaded files per gym (branch photos and documents, product photos, staff documents); 100 MB of uploaded files per member (ID card uploads) - an upload that would pass it is refused and its file removed | caps `storage_mb_per_gym`, `storage_mb_per_member` in `app/core/caps.py` (setting `CAPS_OVERRIDES`, in MB) |

</details>

## Quick start

Requirements: Python **3.10+** (3.12 recommended), ~2 GB of disk (more with the Docker AI failsafe) and internet for
the first setup. The setup scripts install everything else - PostgreSQL + pgvector, Redis, Tesseract - and are **safe
to run again**: finished steps are skipped, existing passwords and data are kept.

### macOS / Linux

```bash
./setup.sh      # = --db runtime: PostgreSQL 18 + pgvector, Redis and Tesseract under ~/.ironpulse (no sudo, no Docker)
./run.sh        # starts the database (and Redis) of your setup mode, then the app
```

Then open **http://localhost:10000** - the `PORT` in `.env` (10000 in a fresh `.env` made from `.env.example`). The demo
accounts and their passwords are in `var/demo_credentials.txt` ([Demo data](#demo-data)).

<details>
<summary>All setup options - Docker, native packages, your own server, AI failsafe, voice</summary>

```bash
./setup.sh                       # default: user-space runtime under ~/.ironpulse (no sudo, no Docker)
./setup.sh --db docker           # PostgreSQL 17 + pgvector (:5433) and Redis (:6380) in Docker
./setup.sh --db native           # Homebrew (postgresql@17 + pgvector) or apt / dnf / pacman / zypper (uses sudo)
./setup.sh --db existing --database-url postgresql://user:pass@host:5432/ironpulse [--admin-url SUPERUSER_URL]
./setup.sh --with-ai-docker      # + Gemma failsafe in Docker      --with-voice  + Kokoro voice in Docker
./setup.sh --dry-run             # print the plan only
./setup.sh --skip-pip            # re-run without pip (offline); also --skip-assets / --skip-videos / --skip-seed / --no-dev
./setup.sh --python /usr/bin/python3.12   # the Python 3.10+ interpreter used to create .venv
```

The **runtime** mode downloads [micromamba](https://mamba.readthedocs.io/) and installs PostgreSQL + pgvector,
Redis and Tesseract from conda-forge into `~/.ironpulse/env` - nothing system-wide, works on macOS (Intel and
Apple Silicon) and Linux (x86-64, ARM64):

| What | Where |
|---|---|
| binaries | `~/.ironpulse/env/bin` (postgres, psql, redis-server, tesseract) |
| PostgreSQL data / log | `~/.ironpulse/pgdata`, `~/.ironpulse/postgres.log` - port 5432, socket in `/tmp` |
| passwords (mode 600) | `~/.ironpulse/pg_superuser_password`, `app_db_password`, `redis_password` |
| Redis data / log | `~/.ironpulse/redis` - 127.0.0.1:6379 |
| control | `scripts/runtime_postgres.sh start\|stop\|restart\|status\|psql\|logs` and `scripts/runtime_redis.sh start\|stop\|status\|cli\|logs` |

</details>

`./run.sh` first starts the runtime database and Redis, or the Docker containers, for your setup mode, and the local
AI fallback in the background:

```bash
./run.sh                    # start the app (host/port from .env)
./run.sh --reload           # development: restart automatically when code changes
./run.sh --workers 4        # several worker processes (uvicorn; live events fan out via PostgreSQL) - see the note below
./run.sh --lan              # use it on your phone: HTTPS on this computer's LAN address
./run.sh --host 0.0.0.0     # plain HTTP for other devices (prefer --lan; add the IP to ALLOWED_HOSTS)
./run.sh --port 11001       # override PORT from .env
./run.sh --no-services      # don't start PostgreSQL / Redis first
./run.sh --no-ai-docker     # don't start the local AI fallback (Ollama)
./run.sh --dry-run          # print what would be started and run; start nothing
```

`--reload` cannot be combined with `--workers` or `--lan`, and `--lan` binds every interface itself (leave out `--host`).

> [!NOTE]
> **Several workers need Redis.** With `--workers N` each worker process holds only the sockets of the pages it
> serves, and the next page a user opens may land in another one. Redis (`REDIS_URL`) is how the workers see each
> other's users. Without it (or while it is down) the app fails safe: closing every tab no longer counts as leaving,
> so AI work is only stopped by signing out, by revoked sessions and by closing the page that asked for it - and each
> worker says so in its log at startup. A single worker without Redis loses nothing.

### Windows 10 / 11

Open **PowerShell** in the project folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
powershell -ExecutionPolicy Bypass -File run.ps1
```

`setup.ps1` uses **winget** to install what is missing - Python 3.12, Tesseract OCR (UB-Mannheim) and Docker Desktop -
creates `.venv`, writes `.env` with fresh secrets, starts PostgreSQL 17 + pgvector and Redis in Docker, starts the
Gemma failsafe (Ollama) and pulls `gemma3:4b`, initialises the database, downloads the Windows Tailwind CLI, builds the
assets, generates the videos and loads the demo data. Then open **http://localhost:10000** (the `PORT` in `.env`).

| `setup.ps1` option | Meaning |
|---|---|
| `-DbMode docker` (default) | PostgreSQL 17 + pgvector (port 5433) and Redis (port 6380) in Docker Desktop |
| `-DbMode native` | PostgreSQL 17 via winget + **pgvector built with Visual Studio 2022 Build Tools** (asks for Administrator once - UAC) |
| `-DbMode existing -DatabaseUrl postgresql://user:pass@host:5432/ironpulse` | your own server (`-AdminUrl` = optional superuser URL) |
| `-NoAiDocker` | skip the Docker Gemma failsafe (on by default; `-WithAiDocker:$false` does the same) |
| `-WithVoice` | also start the Kokoro voice for the live coach |
| `-SkipAssets`, `-SkipVideos`, `-SkipSeed` | skip those steps |
| `-SkipPip` | don't run pip (packages already installed / offline re-run) |
| `-PythonVersion 3.12` | Python version to install/use |

`run.ps1` takes `-Reload` while developing, `-Workers 4` for several processes, `-Lan` for your phone,
`-BindHost 0.0.0.0` (plain HTTP on your LAN), `-Port`, `-NoServices`, `-NoAiDocker` and `-DryRun` - the same as
`run.sh`'s options. Helpers in `scripts\windows\`: `services.ps1 start|stop|status|logs|down [-Ai] [-Voice]` (Docker
services), `install_pgvector.ps1` (native pgvector build), `open_firewall_port.ps1 -Port 8000` (LAN access, see below).

> Docker Desktop needs WSL 2; the first install may ask you to restart Windows - just run `setup.ps1` again.
> Without Docker, Redis is optional: sessions fall back to PostgreSQL. If you want Redis anyway use
> [Memurai](https://www.memurai.com/) (`winget install Memurai.MemuraiDeveloper`) or Redis inside WSL and set `REDIS_URL`.

### Docker only

Everything (database, Redis, Gemma failsafe, voice and the app) can run in Docker. You still need a `.env`:

```bash
python3 scripts/make_env.py                       # secrets             (any Python 3 - standard library only)
python3 scripts/manage.py configure --db docker   # DB/Redis passwords  (standard library only, too)
# no Python at all?  docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
#   "python scripts/make_env.py && python scripts/manage.py configure --db docker"
docker compose --env-file .env -f docker/compose.yml --profile app up -d --build      # app on http://localhost:10000
docker compose --env-file .env -f docker/compose.yml up -d ollama ollama-pull         # + Gemma failsafe
docker compose --env-file .env -f docker/compose.yml --profile voice up -d kokoro     # + live coach voice
```

The `app` service runs `manage.py init-db` (role, database, extensions, tables) before `python -m app`. All ports
are bound to `127.0.0.1`. GPU (NVIDIA) for Ollama and Kokoro: add `-f docker/compose.gpu.yml` after the first `-f`.

| Service | Image | Host port | Notes |
|---|---|---|---|
| `db` | `pgvector/pgvector:0.8.7-pg17` | 5433 | superuser `postgres` / `POSTGRES_PASSWORD`; app role `ironpulse` created by init-db |
| `redis` | `redis:7-alpine` | 6380 | `--requirepass ${REDIS_PASSWORD}` `--appendonly yes` |
| `ollama` | `ollama/ollama:0.40.15` (pinned) | 11434 | Gemma failsafe, OpenAI-compatible `/v1`; 6 GB memory limit, healthcheck, logs capped ([Local fallback LLM](#local-fallback-llm)) |
| `ollama-pull` | same | - | one-shot pull of `AI_DOCKER_MODEL` (gemma3:4b), skipped when it is already in the volume |
| `kokoro` | `ghcr.io/remsky/kokoro-fastapi-cpu:v0.9.2` | 8880 | profile `voice` |
| `app` | `docker/Dockerfile` (python:3.12-slim + Tesseract, non-root) | `PORT` | profile `app`; reaches Ollama at `http://ollama:11434/v1` |

### Use it on your phone (LAN)

```bash
./run.sh --lan                                            # macOS / Linux
powershell -ExecutionPolicy Bypass -File run.ps1 -Lan     # Windows
```

LAN mode serves IronPulse to every device on your Wi-Fi - **HTTPS only**, on the `PORT` from `.env` (11000 in the
examples below):

1. It finds this computer's LAN address (macOS: `ipconfig getifaddr en0` / `en1`) and its Bonjour name
   (`scutil --get LocalHostName` -> `<name>.local`) and makes a certificate for exactly those names and `localhost`,
   signed by a small local certificate authority kept in `var/tls/` (git-ignored; keys readable only by you). When
   the LAN address changes, the next start makes a new certificate - the CA stays the same.
2. It prints the addresses to open on the phone, e.g. `https://192.168.0.227:11000` and
   `https://my-macbook-air.local:11000` (and a QR code when the `qrcode` package or `qrencode` is installed).
3. **Once per phone**, install the CA file it names, `var/tls/ironpulse-lan-ca.crt` (AirDrop or mail it to the phone):
   - **iPhone:** open the file -> Settings > *Profile Downloaded* > Install; then Settings > General > About >
     *Certificate Trust Settings* > turn on full trust for "IronPulse LAN CA".
   - **Android:** Settings > Security > More security settings > Encryption & credentials > *Install a certificate* >
     *CA certificate* > pick the file (Chrome trusts it; Firefox needs "Use third party CA certificates").

   The CA is name-constrained to private addresses, `*.local` and `localhost`: even a stolen CA key could not
   impersonate any public website on a phone that trusts it.
4. macOS asks once whether Python may accept incoming network connections - choose **Allow**.

- The development server on the same port must be stopped first (LAN mode refuses to start next to it), or use
  another port: `./run.sh --lan --port 11001`.
- `python scripts/lan.py --dry-run` shows the addresses, certificate and settings without starting anything;
  `--renew` makes a new server certificate; `--mkcert` uses mkcert's CA instead (if your phones already trust it).
- Android often cannot resolve `.local` names - open the IP address there.
- Use it on networks you trust (home, office): everyone on the same Wi-Fi can reach the sign-in page - behind the
  app's rate limits, bans and lockouts, like any public site.
- Windows: allow the port once in an Administrator PowerShell:
  `powershell -ExecutionPolicy Bypass -File scripts\windows\open_firewall_port.ps1 -Port 11000`.

<details>
<summary>What LAN mode sets, and why</summary>

LAN mode passes its settings as environment variables (`.env` is never edited): `DEBUG`/`RELOAD` off (no API docs),
`COOKIE_SECURE=true` (`__Host-` Secure cookies), `FORCE_HTTPS=true`, `TRUST_PROXY_HEADERS=false` (devices connect
directly, so rate limits and bans apply per device address), `ALLOWED_HOSTS` = exactly the LAN address(es),
`<name>.local` and `localhost` (any other host name gets 400 - DNS rebinding included), `PUBLIC_BASE_URL=https://<name>.local:11000`,
empty `CORS_ORIGINS` and `RATE_LIMIT_WHITELIST`. The CSRF and WebSocket origin checks then accept only those origins.
TLS 1.2 and 1.3 with forward-secret AEAD ciphers only; HSTS without `includeSubDomains` - and none for `localhost`,
where a browser would pin every localhost port (the development server included) to HTTPS. `ENVIRONMENT=production`
works the same way: its start-up checks run on these settings.

</details>

### Turn on the local AI fallback

When the AI gateway PC is off, a local Gemma keeps the AI features answering (slower, one at a time) instead of
"AI unavailable". Enable it once, either way:

- **Docker** (Docker Desktop on macOS / Windows, Docker Engine on Linux): `./setup.sh --with-ai-docker` (`setup.ps1`
  does it by default). It downloads the Ollama image and the model (about 3.3 GB) in the background.
- **Native Ollama** (on a Mac it uses the Apple GPU - much faster than Docker's CPU): install it from ollama.com.

```bash
./setup.sh --with-ai-docker                  # once: Ollama + gemma3:4b in Docker
./run.sh                                     # from then on it starts in the background - the app never waits for it
python scripts/manage.py ai-status           # every provider: up, model pulled, 1-token latency
```

From then on `./run.sh` / `run.ps1` start it by themselves **in the background** - the app never waits for it - and
print one line saying what they did: Ollama already answering; `docker compose up -d ollama ollama-pull` (the pull is
skipped when the model is already in the `ollama` volume, so it also starts offline); Docker Desktop started first
when it is installed but not running; `ollama serve` + `ollama pull` for a native Ollama; or which of the two to
install. Its log: `var/logs/ai-fallback.log`. `./run.sh --no-ai-docker` (`run.ps1 -NoAiDocker`) skips it; by hand:
`docker compose --env-file .env -f docker/compose.yml up -d ollama ollama-pull` (Windows also
`scripts\windows\services.ps1 start -Ai`). Memory, other models and how failover works: [Local fallback LLM](#local-fallback-llm).

## Features by role

### Members

- **Find a gym** - directory with search, city filter and "Use my location" distance sorting; branch pages with a
  photo gallery, map, opening hours, features, trainers and staff (with photos) and the owner.
- **Memberships** - up to 12 plan types per gym: offline, offline + trainer, offline + AI, offline + trainer + AI,
  the same four online, and four combo (online + offline) plans. Pre-recorded workout videos are included in every
  plan. Offline and combo plans are only sold within **30 km** of a published branch (GPS, city fallback); farther
  away you are asked to visit the gym owner. Offline checkout is completed **at the branch desk** with a 6-digit owner
  OTP (10 minutes, 5 attempts) and a 1.5 km presence check when GPS is shared.
- **Gym membership limits** - one person holds memberships at most at **3 gyms online** (online or combo plans) and
  **1 gym in person** (gym or combo plans) at a time; renewing or switching plans at a gym you already hold never
  counts. The checkout, the owner's desk and the activation all check it (see [Limits](#limits)).
- **AI coach** - personalised workout plans that respect your recorded health conditions (contraindicated exercises
  are removed by deterministic rules), a calendar, tasks and an assistant that can act on them. Every AI feature uses
  what you told the app once - see [What the AI knows about you](#what-the-ai-knows-about-you).
- **AI plan advisor** - ask "which plan fits me?"; the answer is **streamed**, so the first words appear in about a
  second. Prices and plan names always come from the database, never from the model.
- **Health & safety** - metrics, scale and medical-report scans, wellness checks, protective-gear suggestions and a
  safety suspension on red flags - see [Health & safety](#health--safety).
- **Live AI voice coach** - an animated coach face with emotions and lip-sync that talks you through a workout - see
  [Live AI voice coach](#live-ai-voice-coach).
- **Gym shop** - products (supplements, apparel, equipment), wishlist, cart, coupons, checkout, order tracking,
  cancellations and refund requests, verified-purchase reviews, receipts, allergen/condition warnings, 18+ products
  blocked for minors, and **AI product Q&A** ("Ask AI about this product", streamed).
- **Live chat** with a gym's trainers or admin team (WebSocket), misconduct reports, notifications in-app **and** by
  e-mail (a guardian gets a copy for minors).

### Trainers and staff

- **Joining a gym** - a gym invites you; you link **your own account**, upload a photo and a verified ID (trainers also
  documents) before the owner approves you.
- **Staff portal** (`/staff`) - your employments and what each still needs before activation, today's shifts in each
  branch's time zone, the week ahead, assigned clients and certificate uploads; `/staff/schedule` is a Mon-Sun grid
  across every employment.
- **Clients** (`/staff/clients`) - while an active assignment links you: the client's goal, level, health conditions,
  allergies and injuries, latest metrics + BMI, active workout plan and suspension status, with the trainer-safety
  panel on top. A member who keeps health details private shows "The member chose not to share health details - ask
  before training", and none of their health data is read.
- **Chat with members** - condition-aware warnings before a message is sent and urgent red-flag alerts - see
  [Trainer safety warnings and red flags](#trainer-safety-warnings-and-red-flags).

### Gym owners (gym admins)

- **Gyms & branches** - details, hours (max 14 h a day), features, map pin, documents, photos. A branch is listed
  only after **AI verification**, while open, with offline registration enabled and **at least 3 photos**.
  Online-only gyms, closing and reopening (no re-verification).
- **Staff** - invite staff who must link **their own account**, upload a photo and a verified ID (trainers also
  documents) before approval. Shift rules: a trainer's shifts at different branches are **6 h apart**, and one
  brand's branches more than **50 km** apart cannot share a trainer. Ending a job always takes a reason - see
  [Staff removal and bans](#staff-removal-and-bans-trust--safety).
- **Jobs** - job postings on the public jobs board. Enquiries are for **job seekers only**: guests, members and former
  staff enquire; a signed-in gym owner, super-admin or anyone with a current staff job (active or suspended, at any gym)
  sees why instead of the form, and the route refuses them before any spam accounting.
- **Payroll** - salaries counted by **days worked**, gratuity and tax-withholding estimates per country,
  Cashfree payouts (simulated in test mode), payment proofs, final settlements. Staff can be removed only when
  every salary up to date is paid.
- **Members** - register members manually or let the AI turn free text into a form (**AI intake**), review safety
  suspensions, read every chat of the gym, answer misconduct reports, audit log. A gym sees a member only while the
  member agreed to a current membership there (active or suspended, a purchase they started in their own app, or one
  that ended within the last 90 days) or the owner registered them. The desk records cash only for a member who chose
  that plan in their own app, holds a current membership there or was registered by the owner - never a stranger.
- **Plans, desk OTPs, the shop dashboard** (products, stock, orders, refunds, coupons, review moderation).

### Platform super-admin

Security centre (live security events, bans, AI-assisted triage), review queues, AI provider status
(`/superadmin/ai`), users, the e-mail outbox, the audit log and a self-check page. **Trust & safety**
(`/superadmin/trust`): account bans (lifted with a required note), staff conduct violations, the network addresses a
severe ban acted on, and the blocked-identity registry - type an e-mail or phone to look it up (it is hashed the same
way and matched, never stored). **Load test** (`/superadmin/load-test`): the latest stress-test verdict, capacity,
latencies and server resources ([docs/load-test-page.md](docs/load-test-page.md)). **Governance**
(`/superadmin/governance`): ban owners or staff and delete gyms with failsafes - see [Platform admin](#platform-admin).

### Platform admin

**Sign in at `/superadmin/login`** - the small *Platform admin sign-in* link under the normal sign-in page. It is the
only place a platform admin (super-admin) can sign in: the account is `SUPERADMIN_EMAIL` with `SUPERADMIN_PASSWORD`
from `.env`. To change the password, edit `SUPERADMIN_PASSWORD` in `.env` and restart IronPulse - it applies at
start-up, every admin session is signed out, and the password is never logged or shown. The normal sign-in page, the
account chooser, "Forgot password", invitations and the account settings never accept or change an admin's
credentials: they answer exactly as for an unknown account.

**Failed attempts ban the address.** Five failed attempts from one network address within 15 minutes - on the admin
page, or with an admin's e-mail on the normal sign-in page - ban that address for **24 hours** on every page (a second
ban within 30 days is permanent). The server machine itself (`127.0.0.1` / `::1`) is never banned, only counted - so
the owner can always get in from the server (five wrong passwords still pause the account itself for 15 minutes, like
any account; `python scripts/manage.py create-superadmin --reset-password` clears that). To lift a ban from the
server's terminal:

```bash
.venv/bin/python scripts/manage.py unban 203.0.113.7     # Windows: .venv\Scripts\python scripts\manage.py unban ...
```

Optionally allow the admin page and console only from your own networks:
`SUPERADMIN_ALLOWED_IPS=203.0.113.7,10.0.0.0/8` (behind a reverse proxy, list it in `TRUSTED_PROXIES` so the real
client address is used - a typed `X-Forwarded-For` never counts).

**Governance** (`/superadmin/governance`, and *Ban…* on the Users page):

- **Ban a gym owner** - their account is banned (a severe reason also blocks their recent network addresses) and every
  gym of theirs is **suspended at once**: hidden from public pages and search, no new memberships, orders or job
  enquiries. You choose whether members keep access until their paid period ends or get a **pro-rata refund**. Staff
  are told; their earned pay keeps being recorded and is paid from the gym's settlement desk.
- **Ban an employee or trainer** - a **final settlement** is created for each job first (earned pay is never lost),
  then every employment ends with the reason, their shifts are cancelled, their clients move to the trainer you pick
  (or are told and unlinked) and their sessions end.
- **Delete a gym, in two steps** - *schedule*: the gym is suspended at once and can be restored for
  `GYM_DELETE_COOLING_OFF_DAYS` (7) days; *final deletion*: **blocked** until every employee's final settlement is paid
  (Cashfree payout, or a recorded payment proof when payouts are off) or waived with a written note, members are
  refunded pro rata or their memberships have ended, and open shop orders are cancelled and refunded. The gym is then
  archived - transactions, salary statements, invoices and refunds are **never deleted**.
- **Lift a ban / restore a gym** - with a note; an owner's suspended gyms open again with the lift.

Every action shows its **impact first** (members, refunds due, staff, earned pay outstanding, payouts under way, open
orders), then needs the account's e-mail or the gym's name **typed out**, a reason and a note. Money moves only when
you confirm it; actions are rate-limited, audited (`governance.*` in the audit log) and notify everyone affected. A
platform admin can never ban themselves or another platform admin.

**Site overview with AI** (`/superadmin/insights`, *Site overview* in the admin menu) - what is going on across
IronPulse and what to fix first:

- **The numbers** - sign-ups, active members, orders and revenue; security (brute force, 404 scans, admin sign-in
  failures, the busiest addresses, bans); the AI guard's **frequent limit offenders**; **spam** (job-enquiry spam,
  limits hit, who was refused most); trust & safety; review queues; unpaid salaries, final settlements and refunds in
  ₹; stuck background jobs; health red flags; the latest load test. Counts, member IDs and masked e-mails only,
  refreshed every 2 minutes.
- **Fix first** - IronPulse's own rules turn those numbers into a critical / high / medium list, each with its numbers
  and a link to the page where you act. It works without AI and without JavaScript; its top 3 also show on
  `/superadmin`.
- **Ask the AI** - pick a suggestion ("What's going on today?", "What should I fix first?", "Who are the frequent limit
  offenders?", "Any spammers this week?") or type your own. The AI answers only from the same figures - it never runs
  queries or takes actions - and when AI is off or busy you get the automatic overview. At most 10 questions a minute
  per admin; every question is audited (`insights.ask`).

### Accounts, profiles and sessions

- Sign in with your **e-mail, @username, phone number or member ID** (e.g. `IP-000123`) plus password;
  "Forgot password" and "Forgot username" e-mail the details to the address on the account only.
- Every account except the super-admin uploads a **government ID card** (read by several OCR engines,
  encrypted at rest). Owners and staff also need a photo. Under-18s need a guardian, who is copied on everything.
- **Profile pages** (`/profile/<id>`) with privacy levels: owners and active staff are public (they appear on gym
  pages); members are private - only the member, their gym's managers, their assigned trainers and the platform
  admin can see them (others get a 404).
- **Redis-backed sessions** with PostgreSQL as the source of truth: Redis caches *session -> user id* only, so the
  Google-style account chip in the navigation always shows your **latest name and photo from PostgreSQL**.
  Changing your password signs you out everywhere. Without Redis, sessions simply live in PostgreSQL.
- **One session per account** (signing in on a new device signs the old one out), **double-submit protection** (a
  double click, a resent form or Back-and-submit runs once and shows the same outcome) and a **sign-in account
  chooser** like Google's (the device keeps only a random reference per account; the password is always asked) -
  details in [Passwords and sessions](#passwords-and-sessions).

## AI

> ⚠️ AI answers, coach plans and live coaching can be wrong and are not medical, legal or financial advice - see
> [Disclaimer & no liability](#disclaimer--no-liability).

### AI rules (everywhere)

- **AI is available only to signed-in users.** Guests can browse; AI answers, plans and advice need an account.
- Every AI answer carries the disclaimer **"AI can make mistakes. Double-check important information - this is not
  medical, legal or financial advice."** JSON APIs return it as `"disclaimer"`.
- Safety- and money-related AI output is re-checked by deterministic code (exercise contraindications, payroll
  arithmetic, suspension thresholds, ID validation, shop safety flags). When every provider is down the app says
  **"AI unavailable"** and everything else keeps working.
- IronPulse **does not measure heart rate or BPM** (or blood pressure). Safety checks use what you type or say.

### What the AI knows about you

Everything a member told the app or the AI **once** is used by every AI feature - the assistant, the AI coach and its
plans, the live voice coach, the plan advisor and the shop's product answers: health conditions, injuries, medications
and allergies (the health profile), and the exercise types they enjoy or avoid, goals, equipment and schedule (the
fitness profile).

- Mention something new in a chat ("I have asthma", "I take Metformin 500 mg") and the AI offers **"Remember this?"** -
  nothing is saved without the member's click; a medication is saved by its name and dose, never the schedule words.
- **`/me/health/profile/ai`** ("What the AI knows about you") lists everything the AI uses. Edit anything, or make the
  AI forget it - it is gone from the very next answer. Only the member sees this page.
- Saved words reach the AI as quoted data. Text that reads like an instruction ("ignore the rules", "SYSTEM:") is
  withheld - also when written with look-alike, full-width or invisible characters. Safety-relevant entries come first.
- Limits: 50 health entries per member; training preferences, memory offers and prompt sizes in [Limits](#limits).

### AI providers and failsafes

Every AI call goes through `app/ai/client.py`, which tries the providers **strictly in this order** with a
circuit breaker per provider ([diagram](#ai-provider-failover)):

1. **Gemma via your gateway** - an OpenAI-compatible llama.cpp gateway, called with the **OpenAI SDK**.
   Configured as `AI_GATEWAY_BASE_URL=http://192.168.0.101:8000/v1` (`AI_GATEWAY_MODEL=gemma`, 8k context). The
   gateway's status page `http://192.168.0.101:8000/status` lists its backends (online, busy slots).
2. **Gemma in Docker** - Ollama with `gemma3:4b` on `http://localhost:11434/v1`, the local failsafe.
3. **Claude** - via the Anthropic SDK when `ANTHROPIC_API_KEY` is set.
4. **"AI unavailable"** - pages show a banner and use deterministic fallbacks; nothing else breaks.

Prompts are compact (Gemma's 8k window, `max_tokens <= 1500`), each provider attempt has a short timeout,
free-text answers stream token by token, and user text is always treated as data inside prompts.

### AI guard and fair queue

Before any provider is asked, every AI call passes the AI guard and a fair queue - the contract is
[docs/ai-guard-and-queue.md](docs/ai-guard-and-queue.md), the numbers are in [Limits](#limits):

- **Guard** (`app/ai/guard.py`) - per-user quotas per minute / hour / day by account tier and feature group, spam
  checks (repeated characters, link floods, mostly symbols, over-long text), duplicate questions and escalating
  cooldowns; long cooldowns are reported to the security centre. With Redis the counters are shared by every worker;
  on a Redis error they fall back to memory, so the limits never fail open.
- **Fair queue** (`app/ai/scheduler.py`) - bounded concurrency per process, round-robin between users within a
  priority (realtime > interactive > background, with ageing), per-user and whole-queue caps, then "AI is busy". Time
  in the queue never counts as a provider failure.
- **Stop when you leave** (`app/services/presence.py`) - closing the tab, navigating away, losing the connection or
  signing out stops the AI work nobody will see; user-owned background work waits until the user is back.
- **Charged only once it starts** - refused, busy, queued-then-stopped and failed calls are free.

### Local fallback LLM

**What it is.** Gemma 3 (4B) running on this computer in [Ollama](https://ollama.com) - provider `docker`, second
in `AI_PROVIDER_ORDER=gateway,docker,anthropic`. When the gateway PC is off or broken, members still get answers
(slower, one at a time) instead of "AI unavailable". To enable it, see
[Turn on the local AI fallback](#turn-on-the-local-ai-fallback).

**RAM.** gemma3:4b with the 8k context needs about 4.5 GB. The container may use 6 GB (4 GB reserved) - set
`OLLAMA_MEMORY_LIMIT` / `OLLAMA_MEMORY_RESERVATION` in `.env` for a bigger model - so give Docker Desktop's VM at least
8 GB (Settings > Resources). An NVIDIA GPU: add `-f docker/compose.gpu.yml` (or uncomment the block in
`docker/compose.yml`). Its logs are capped at 3 × 10 MB.

**Change the model.** `AI_DOCKER_MODEL=gemma3:12b` (and a larger `OLLAMA_MEMORY_LIMIT`) in `.env`, then `./run.sh` -
it pulls the new model in the background. The app reaches Ollama at `AI_DOCKER_BASE_URL`: `http://localhost:11434/v1`
when the app runs on this computer, `http://ollama:11434/v1` when the app runs in the same compose project
(`--profile app` sets it).

**How failover works.** Every AI call tries `gateway`, then `docker`, then `anthropic` (with `ANTHROPIC_API_KEY`),
then the feature's own fallback (a deterministic answer or "AI unavailable"):

- a provider that refuses the connection, answers HTTP 5xx or something that is not a valid answer is skipped at once;
  one that does not accept the connection within **2 s** (a switched-off PC; `CONNECT_TIMEOUT_SECONDS`) after 2 s; one
  whose first words do not arrive within the feature's first-token deadline (8-12 s) at that deadline;
- an answer that breaks off **before** its first words is retried on the next provider; one that breaks off **after**
  them ends with "… (the answer was interrupted)" - it is never continued by another provider's answer - and one that
  hits its length limit ends with "…" (neither is cached as a complete answer);
- 2 failures in a row (`AI_CIRCUIT_FAILURES`; a provider that keeps breaking off mid-answer included) open the
  provider's circuit breaker: it is skipped for 45 s (`AI_CIRCUIT_COOLDOWN_SECONDS`), then tried again, and a success -
  or the health check every 60 s - closes it;
- Ollama runs one answer at a time (`AI_DOCKER_MAX_CONCURRENCY=1`): waiting members take turns - one person's second
  question never goes before somebody else's first;
- the AI quota is charged once per question, whichever provider answered; the super-admin **AI** page names the
  provider of every call.

**Verify.** `python scripts/manage.py ai-status` (each provider: running, model pulled, the latency of a 1-token
answer), `python scripts/manage.py ai-test --provider docker` (a real answer through the app's own provider code), the
super-admin **AI** page ("Test the providers"). On a busy laptop CPU, Ollama in Docker can need 5-15 s before its
first words - longer than the interactive features' first-token deadlines, so it then mostly serves background work:
prefer the GPU gateway, a native Ollama, or a GPU for Docker.

**Gateway on another PC of your LAN (Windows firewall).** The PC that runs the gateway must accept inbound
connections on its port (8000). In an **Administrator** PowerShell on that PC:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\open_firewall_port.ps1 -Port 8000
# equivalent:  New-NetFirewallRule -DisplayName "Gemma gateway 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet -Profile Private
```

The rule only accepts your local subnet on *Private* networks; remove it with `-Remove`. Test from the IronPulse
machine with `python scripts/manage.py ai-status` or `curl http://192.168.0.101:8000/v1/models`.

### Talk to Gemma from the terminal

The OpenAI SDK, with the same gateway and key as the app:

```bash
python scripts/manage.py ai-status                                   # every provider: up, model pulled, 1-token latency
python scripts/manage.py ai-test --provider docker                   # one real answer through the app's own code
python scripts/manage.py ai-chat "Plan a 20-minute warm-up"          # one question, streamed
python scripts/manage.py ai-chat                                     # interactive chat (/reset, /exit)
python scripts/manage.py ai-chat --base-url http://192.168.0.101:8000/v1 --model gemma "Hello"
python scripts/manage.py ai-chat --provider docker "Hello"           # the Ollama failsafe instead
```

### Live AI voice coach

`/me/live` starts a guided workout with a talking coach:

- **Animated face** drawn on a canvas - emotions (happy, encouraging, calm, concerned, proud, focused ...),
  blinking and breathing, and **lip-sync** while it speaks. Respects "reduce motion".
- **Voice** from **Kokoro** (self-hosted TTS in Docker, `TTS_BASE_URL=http://localhost:8880/v1`, voice `af_heart`);
  when Kokoro is not running the **browser's built-in voice** is used automatically.
  Start it: `docker compose --env-file .env -f docker/compose.yml --profile voice up -d kokoro`
  (`./setup.sh --with-voice`, `setup.ps1 -WithVoice`) - or the live-coach package's standalone
  `docker compose -f docker/kokoro.compose.yml up -d`. Use one of the two: both publish port 8880.
- **Speech recognition** by your browser's speech service (e.g. Chrome or Safari); IronPulse never records or
  stores audio. A **mic toggle** lets you switch to typing at any time.
- **Voice commands** - pause, resume, next/skip, set done, change the exercise or workout, easier/harder, quit.
- **Safety stop** - reporting chest pain, trouble breathing, fainting and similar red flags stops the session
  immediately (deterministic rules run *before* any AI), shows emergency guidance, pauses gym activities until
  an admin reviews and notifies the gym (and the guardian of a minor). Discomfort swaps in a safer exercise.
- The coach **does not measure heart rate or BPM**; it only reacts to what you say or tap.

## Health & safety

> ⚠️ Health checks, safety pauses, gear suggestions, warnings and red-flag alerts are aids that can miss things - not
> medical advice and not a guarantee. See [Disclaimer & no liability](#disclaimer--no-liability).

- **Health tracking** - log weight, height and other metrics, scan a scale display or a medical report, wellness
  checks (stress score, red-flag symptoms), room AC and bedtime advice.
- **Safety suspension** - implausible jumps or red-flag symptoms trigger a **safety suspension**: gym activities pause
  until a gym admin reviews and reactivates you (with notifications to you, your gym and - for minors - your guardian).
- **Protective-gear suggestions** - conservative, rule-based suggestions from your conditions (back support or
  lifting belt for back problems, gloves for grip/skin problems, wrist wraps, knee sleeves) that never suggest
  gear that conflicts with a condition (e.g. no lifting belt with a hernia). Not a prescription.
- IronPulse **does not measure heart rate, BPM or blood pressure**, and it is not a medical device. Safety checks use
  what you type or say.

### Trainer safety warnings and red flags

> ⚠️ Warnings and red-flag alerts help staff; they can miss things and are not a guarantee - see
> [Disclaimer & no liability](#disclaimer--no-liability).

Trainers and owners are warned about a member's health conditions wherever they deal with the member - the chat, the
staff portal's client page and the owner's member page - shown **only** to people the app already lets see that
member's health data:

- **Full panel** (the owner / admin team of the member's gym and the member's assigned trainers): safety tags in plain
  words, what to avoid, allergies, medications, injuries, notes for trainers and a safety pause.
- **Other staff** in a conversation get only "check with their trainer"; with "Share my health and safety information
  with my gym's trainers" switched off, staff see that it is withheld. Suspended, former and unverified staff see
  nothing - their conversations close.
- **Before a staff or owner message is sent**, it is checked against the member's conditions: suggesting an exercise or
  load a condition rules out ("box jumps" with a knee injury, a 1RM deadlift with a back problem, hot yoga or a sauna in
  pregnancy) shows a warning naming the condition, with **Edit** or **Send anyway** (audited with the tag only).
  Typos and phrasings like "don't forget your box jumps" count; "no box jumps" does not. Deterministic - no AI.
- **Red flags**: a member's message about chest pain, fainting, severe breathlessness and similar shows emergency
  guidance at once (112 in India) and sends the gym admin, the staff member and - while the member shares health
  details - their trainers an urgent alert (once per conversation per hour). It is never refused: no chat limit
  applies, a closed conversation is reopened, and when the chosen staff member is away (suspended or left) the gym team
  gets it instead - also from the AI assistant.

## Security & privacy

### Web security, uploads and the security gate

- Strict **Content-Security-Policy** with a per-request nonce (no inline scripts, styles or handlers), HSTS on
  HTTPS, `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy and Permissions-Policy headers on every page. The answers
  given before the page code runs - the Host check's 400, the HTTP->HTTPS redirect and the 500 page - still carry the
  CSP, HSTS, `X-Frame-Options`, `nosniff`, Referrer-Policy and cross-origin isolation headers from an outermost
  fallback layer (`app/main.py`).
- **Uploads** are content-sniffed and re-encoded. An image's pixel count is checked on its header before any pixel
  is decoded (25 megapixels; a plain phone JPEG up to 64, decoded at reduced scale), and each worker decodes at most 2
  images at once - a 380 KB "decompression bomb" PNG of 100 megapixels is refused instead of taking a gigabyte. A PDF
  with scripts, launch actions, embedded files or XFA forms is refused, judged on the names as PDF readers decode them
  (`/J#61vaScript` is `/JavaScript`), on the file as PDFium re-writes it (objects hidden in compressed object streams
  come out in the open) and on PDFium's own count of scripts and attachments.
- **CSRF** protection on every state-changing request (signed double-submit token bound to the session), same-origin
  CORS, TrustedHost checking. A page left open while the user signed in or out in another tab goes back to that page
  with "This page was out of date ... nothing was changed" (a sign-in page simply signs in) - never an error page and
  never a threat point.
- **Security gate** in front of every request: rate limits (general, sign-in, AI, uploads), request size limits,
  attack-signature scanning (path, query, user agent, small bodies), 404-flood detection and **IP bans** -
  2-24 hours for members depending on severity, permanent for repeat offenders without a membership - plus
  AI-assisted triage that can only act within that policy.
  - **Only evidence a visitor sent themselves counts.** What a browser was *made* to send - a cross-site image, a
    no-cors POST, a link in a chat (`Sec-Fetch-Site` cross-site / same-site / none, a foreign or `null` Origin, any
    browser GET) - is refused and logged as "Not attributable", but never scored, banned or triaged: another web page
    cannot get a visitor's address banned. Tools without browser metadata, the app's own POST forms and scanner user
    agents still score and ban.
  - Events and bans are written every 2 s, each kind in its own transaction. A NUL byte is stored as the text `\x00`,
    a row the database still refuses costs only that row, and a batch that is not committed (a database hiccup) goes
    back to the queue - a ban is confirmed in memory only after it is stored, so it is never lost on a restart.

### Passwords and sessions

- **Passwords** hashed with Argon2id, login lockout after repeated failures. A password change and "sign out every
  other device" end every session of the account - this browser's too, which gets a fresh session in the same step -
  so a copied session cookie stops working. The lockout holds against guesses sent all at once: a right password
  verified while wrong ones locked the account does not sign in, and an unknown sign-in name is locked under every
  spelling (`@x`, `x`, `X`, phone formats) like a real account.
- **Sessions** are random tokens stored only as SHA-256 hashes (PostgreSQL/Redis); cookies are HttpOnly and
  SameSite=Lax (Secure with `COOKIE_SECURE=true`). PostgreSQL has the last word on every request, so a revoked session
  never signs in again, even through a stale Redis entry.
- **One session per account** (`SINGLE_SESSION_PER_ACCOUNT`, default `true`): signing in ends the account's
  sessions on every other device, so the newest sign-in wins. Sign-ins of one account are serialised, so two at the
  same moment leave exactly one live session, the later one. The other device's live connection closes at once (code
  4409) and its AI work stops. Its next page shows the sign-in page with "You were signed out because your account
  signed in on another device (Chrome on Windows, 13 Nov 2026 at 01:30 IST). If this was not you, reset your
  password." Tabs of one browser and signing in again on the same browser don't count as another device. Each case is
  a security event (category *other*, `/superadmin/security`) and an audit row (`auth.signed_out_elsewhere`). Set
  `SINGLE_SESSION_PER_ACCOUNT=false` to let an account stay signed in on several devices.
- **Double-submit protection** on every state-changing request:
  - Every form has a one-time submit id, rendered by the server, so it works without JavaScript. Script calls send
    one as `X-Submit-Id`.
  - The server runs the first request with a given id. A duplicate (a double click, a resent POST, Back and submit
    again with the same values) waits for it and gets the same outcome for 10 minutes: the same redirect, JSON or
    page. A double-clicked sign-in therefore ends with one session.
  - Ids are scoped to the browser's session, so an outcome can never be replayed to another user.
  - Submit buttons show "Please wait…" while a form is sent.
  - Payments, refunds, orders, payroll, invitations and one-time links are also protected against two different
    tabs by their own row locks or unique keys.
- **Remembered accounts** on the sign-in page, like Google's account chooser:
  - With "Remember this account on this device" ticked (the default), the browser keeps only a random reference
    (localStorage `ip.accounts`: a device key and at most 5 references). It never keeps the account id, name, e-mail,
    photo or password: member ids are sequential (`IP-000123`, `IP-000124` ...), so with them anyone could look up
    anyone's name and photo, while a 256-bit reference cannot be guessed (the server keeps only its SHA-256).
  - The page fetches the account's *current* name, masked e-mail and photo (a signed link that expires after 10
    minutes) for each reference, so a new photo or name shows at once. Banned, deleted or revoked accounts come back
    as "gone" and are removed from the device automatically.
  - Choosing an account still asks for the password. "Remove from this device" also revokes the reference on the
    server.
  - References are revoked on "sign out other devices", a password change or reset, account deletion and a ban.
  - Caps: 5 per device, 10 devices per account, 30 look-ups a minute per network address.

### Data protection

- **Encryption at rest** (Fernet) for ID-card scans and payout details; ID numbers stored masked and hashed with a
  pepper for duplicate detection; EXIF metadata stripped from public photos; private files served only through
  authorised routes with `Cache-Control: no-store`.
- Every query is scoped to its owner (foreign IDs return 404, ids outside the 64-bit range too); sensitive owner/admin
  actions are written to an **audit log** - an owner's log never shows another gym's rows nor the platform's trust &
  safety records, and the platform's own actions show as "IronPulse platform"; every account change is notified in-app
  and by e-mail.
- Names, chat messages and notifications never carry bidi overrides or zero-width junk (a message with nothing visible
  is refused); a name keeps the LRM / RLM marks real scripts need.
- Secrets live only in `.env` (mode 600, ignored by git and Docker); the Docker image runs as a non-root user and
  every container port is bound to `127.0.0.1`.

### Staff removal and bans (trust & safety)

- **A position is never edited in place.** "Change position" ends the job with the reason *Position change* and
  re-adds the person in the new role through a new invitation and verification.
- **Every removal takes a reason** from one fixed list in three groups:
  - *Can rejoin later* (position change, contract ended, resigned, restructuring, relocation): any gym may add them
    again, rehiring included.
  - *Conduct violation* (behavioural issue, policy violation, repeated no-shows, unprofessional conduct), with a short
    note: this owner's gyms can never add the person again - another gym sees only the neutral "This person can't be
    added" - and the person's other pending invitations at this owner's gyms end with it. Violations reported by **2
    different gym owners within a year** ban the account automatically.
  - *Severe violation* (harassment, violence or threats, theft or fraud, sexual misconduct, endangering members): the
    account is banned at once, its recent public network addresses are **IP-banned** for 30 days and the platform team
    is alerted. Addresses many accounts share (gym Wi-Fi, mobile carriers) are flagged for review instead of banned.
- **A ban** disables sign-in, ends every session, suspends the person's other jobs (those gyms learn only that the
  account was disabled) and keeps them off every public page. The super-admin reviews it on **/superadmin/trust** and
  lifts it with a note. An invitation sent before a violation can no longer be accepted or activated.
- **Blocked-identity registry - one person per ID and e-mail, forever.** When an account is deleted (by its holder or
  an admin) or banned, one-way keyed fingerprints of its e-mail (case, `+tag` and Gmail dots ignored), phone and ID
  numbers are kept forever - never the raw values. Sign-up, accepting an invitation, an e-mail or phone change and ID
  verification refuse them with a neutral message. The delete-account page warns about this; [TERMS.md](TERMS.md)
  says so.

### Deletion and retention policy

- Deleting an account is a **soft delete**: the data is kept for **30 days** and shown as "Deleted user" in shared
  history (chats, payroll). During those 30 days the e-mail address, phone number and username stay reserved.
- After 30 days an hourly job **permanently erases** the account; database rules cascade or anonymise related rows
  and stored files (photos, encrypted ID scans) are wiped from disk.
- Financial records (payments, refunds, payroll) are kept in anonymised form where the law requires it.
- An owner with active staff, or an employer with unpaid salaries, cannot remove those people until everything is
  settled.
- Old operational rows (security events, audit log, AI call log, sent or failed e-mail, finished jobs, read
  notifications, lifted or expired IP bans, used one-time links, expired sessions) are deleted nightly after their
  `RETENTION_*_DAYS` - see [Limits](#limits) (Retention).
- `python scripts/manage.py reset-db --yes` wipes **everything** in your own installation (development only).

## Configuration (.env)

`scripts/make_env.py` copies `.env.example` to `.env` and generates the secrets; the setup scripts then add the
connection settings. `.env` is created with mode 600 and is ignored by git and Docker. Every value can also be set
as a real environment variable (which wins over `.env`).

| Key | Default / example | Purpose |
|---|---|---|
| `HOST`, `PORT` | `127.0.0.1`, `10000` (`.env.example`; `11000` when `PORT` is not set at all) | where the app listens (`0.0.0.0` = your LAN; add the IP to `ALLOWED_HOSTS`) |
| `PUBLIC_BASE_URL` | `http://localhost:10000` | absolute links in e-mails |
| `ALLOWED_HOSTS`, `CORS_ORIGINS` | `localhost,127.0.0.1,::1` / empty | trusted Host headers / extra CORS origins |
| `COOKIE_SECURE`, `FORCE_HTTPS`, `TRUST_PROXY_HEADERS` | `false` | set when served over HTTPS / behind a trusted proxy (`true` by default with `ENVIRONMENT=production`) |
| `SECRET_KEY`, `DATA_ENCRYPTION_KEY`, `ID_HASH_PEPPER` | generated | signing key, Fernet key for files at rest, pepper for ID-number hashes - **never change the last two after go-live** |
| `DATABASE_URL` | `postgresql+asyncpg://ironpulse:...@localhost:5432/ironpulse` | app database |
| `DATABASE_ADMIN_URL` | empty | optional superuser URL - only used by `init-db` |
| `TEST_DATABASE_URL` | `.../ironpulse_test` | test database (must contain "test") |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S` | `10`, `5`, `10` s | database connections per worker process, and how long a request waits for a free one before the friendly 503 (see [Several workers and database connections](#several-workers-and-database-connections)) |
| `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_TX_TIMEOUT_MS` | `15000`, `60000` | a request's query running longer / a transaction idle longer is stopped (503 "taking too long") |
| `RETENTION_*_DAYS` | security `90`, audit `365`, AI log `30`, e-mail `30`, jobs `14`, read notifications `90`, IP bans `90`, one-time links `30`, sessions `30` | nightly deletion of old operational rows (`0` keeps a table); see [ARCHITECTURE.md](ARCHITECTURE.md) "Background work" |
| `REDIS_URL` | empty | optional session cache and shared presence, e.g. `redis://:password@127.0.0.1:6379/0` - needed with `--workers N` to stop AI work when users leave |
| `AI_PROVIDER_ORDER` | `gateway,docker,anthropic` | failover order |
| `AI_GATEWAY_BASE_URL` | **`http://192.168.0.101:8000/v1`** | Gemma behind your OpenAI-compatible llama.cpp gateway |
| `AI_GATEWAY_API_KEY`, `AI_GATEWAY_MODEL` | `gemma` | gateway key and model id |
| `AI_GATEWAY_STATUS_URL` | `http://192.168.0.101:8000/status` | gateway status page (backends online/busy) |
| `AI_DOCKER_BASE_URL`, `AI_DOCKER_MODEL` | `http://localhost:11434/v1`, `gemma3:4b` | Gemma failsafe in Docker |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | empty, `claude-opus-5-5` | Claude as the last resort |
| `TTS_BASE_URL`, `TTS_VOICE` | `http://localhost:8880/v1`, `af_heart` | Kokoro voice for the live coach |
| `TESSERACT_CMD`, `OCR_ENGINES`, `OCR_MIN_ENGINES` | auto, all, `2` | OCR configuration |
| `GEOAPIFY_API_KEY` | empty | maps + geocoding (OpenStreetMap fallback) |
| `CASHFREE_ENV`, `CASHFREE_APP_ID`, `CASHFREE_SECRET_KEY`, `CASHFREE_PAYOUT_CLIENT_ID`, `CASHFREE_PAYOUT_CLIENT_SECRET` | `sandbox`, empty | payments; empty = TEST MODE |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`, `SMTP_SSL` | empty, `587` | e-mail; empty host = files in `var/mail/` |
| `RATE_LIMIT_*`, `MAX_UPLOAD_MB` | `240`/min, auth `10`, AI `60`, `8` MB | abuse protection (per network address; see [Limits](#limits)) |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | `250` | a shared-limit Redis call slower than this is judged from the worker's memory, which mirrors the shared state |
| `SINGLE_SESSION_PER_ACCOUNT` | `true` | a new sign-in ends the account's other sessions ("signed in on another device"); `false` lets an account stay signed in on several devices |
| `REALTIME_PG_BRIDGE_URL` | empty (= `DATABASE_URL`) | a direct, session-pooled database URL for the cross-worker LISTEN bridge - needed behind PgBouncer in transaction mode |
| `AI_CONNECT_TIMEOUT_SECONDS` | `2` | how long an AI provider gets to accept the connection before the next one is asked (Claude: at least 5 s) |
| `SUPERADMIN_EMAIL`, `SUPERADMIN_PASSWORD` | `admin@ironpulse.local`, generated | the platform admin (signs in at `/superadmin/login` only); created at startup, and a changed password applies at the next start ([Platform admin](#platform-admin)) |
| `SUPERADMIN_ALLOWED_IPS` | empty (any address) | addresses / CIDR ranges allowed to open `/superadmin/login` and the admin console |
| `SUPERADMIN_LOGIN_MAX_FAILURES`, `SUPERADMIN_LOGIN_WINDOW_MINUTES`, `SUPERADMIN_LOGIN_BAN_HOURS` | `5`, `15`, `24` | failed admin sign-ins per address within the window that ban it, and for how long (loopback is never banned) |
| `GYM_DELETE_COOLING_OFF_DAYS`, `GOVERNANCE_ACTIONS_PER_HOUR` | `7`, `30` | a gym scheduled for deletion stays restorable this long; destructive governance actions per admin per hour |
| `AI_GUARD_ENABLED`, `AI_QUOTA_SCALE` | `true`, `1.0` | per-user AI quotas, duplicate/spam checks and cooldowns ([docs/ai-guard-and-queue.md](docs/ai-guard-and-queue.md)); the scale multiplies every quota |
| `AI_QUEUE_CONCURRENCY`, `AI_TTS_CONCURRENCY`, `AI_QUEUE_MAX_PER_USER`, `AI_QUEUE_MAX_TOTAL` | `4`, `2`, `4`, `200` | the fair AI queue (per process): calls running at once, Kokoro syntheses at once, one user's queued + running calls, whole-queue cap |
| `PRESENCE_GRACE_SECONDS`, `JOBS_CONCURRENCY` | `45`, `2` | how long after their last page closed a user counts as gone (their AI work stops, their pausable jobs wait); background jobs at once per process |
| `TASKS_MAX_OPEN` | `500` | open tasks and reminders one person may hold at once - the task form and the AI assistant say so when it is reached |
| `RUN_BACKGROUND_JOBS`, `SEED_DEMO_DATA`, `RELOAD` | `true`, `true`, `false` | workers (coach plans and AI notes are built by them), demo data, auto-reload |
| `IRONPULSE_DB_MODE` | written by setup | `runtime` / `docker` / `native` / `existing` (used by `run.sh` / `run.ps1`) |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `REDIS_PASSWORD` | generated (Docker mode) | read by `docker/compose.yml` |

Production-only settings (`ENVIRONMENT`, `TRUSTED_PROXIES`, the simulated AI provider) are described in
[Deploying to production](#deploying-to-production); the caps' `CAPS_OVERRIDES` in [Limits](#limits).

### Several workers and database connections

Each worker process has its own database pool, shared by its requests and its background work: up to
`DB_POOL_SIZE` + `DB_MAX_OVERFLOW` connections, plus one LISTEN connection with `REALTIME_PG_BRIDGE` (which
`manage.py run --workers N` sets). The app therefore needs **workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW + 1)** connections, and a
default PostgreSQL accepts 100 minus 3 reserved. The defaults (10 + 5) fit **4 workers**: 4 × 16 = 64 of 97, leaving
room for psql, scripts, backups and a development server. When all of a worker's connections are busy, further
requests wait up to `DB_POOL_TIMEOUT_S` (10 s) and then get a friendly "taking too long - please try again" page (503),
never a database error. Each worker checks this budget when it opens its first connection and logs a WARNING with the
numbers when the pools could claim more than 80 % of PostgreSQL's connections: then lower `DB_POOL_SIZE` /
`DB_MAX_OVERFLOW`, run fewer workers, raise PostgreSQL's `max_connections`, or put PgBouncer in front
([ARCHITECTURE.md](ARCHITECTURE.md) "Database sizing" has the formula and the PgBouncer settings). The worker count is
read from `--workers N`, `WEB_CONCURRENCY` or `python -m app`'s hand-off. A `.env` created before this change may still
pin `DB_MAX_OVERFLOW=20` (4 × 31 = 124 connections): delete that line.

### Payments, maps, e-mail and sessions

- **Payments (Cashfree).** With empty `CASHFREE_*` keys every payment, refund and payout runs in **TEST MODE**:
  simulated, clearly labelled, no real money moves - perfect for demos. To try the real gateway, create a
  [Cashfree](https://www.cashfree.com/) merchant account, copy the **sandbox** App ID/Secret (and Payout client
  ID/secret) into `.env` with `CASHFREE_ENV=sandbox`, and restart. `production` keys move real money.
- **Maps (Geoapify).** Add a free `GEOAPIFY_API_KEY` from <https://myprojects.geoapify.com> for map tiles, search
  and reverse geocoding. Without it tiles and search fall back to OpenStreetMap.
- **E-mail (SMTP).** Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` (and `SMTP_STARTTLS`
  or `SMTP_SSL`). Without SMTP every e-mail is written to **`var/mail/*.eml`** (open them with any mail client) -
  the super-admin's outbox page shows them too.
- **Redis sessions.** `REDIS_URL` enables the fast shared session cache (set automatically by the Docker and
  runtime setups). The account chip always reads the latest name/photo from PostgreSQL. If Redis goes down the app
  falls back to PostgreSQL sessions for 30 seconds at a time - nobody is signed out.

### OCR engines per operating system

ID cards (and scale displays / medical reports) are read by several engines; at least `OCR_MIN_ENGINES` (2)
must agree for automatic verification, otherwise the document goes to manual review.

| Engine | Windows | macOS | Linux |
|---|---|---|---|
| Tesseract | `winget install UB-Mannheim.TesseractOCR` (set `TESSERACT_CMD`) | Homebrew or the runtime (`~/.ironpulse/env/bin/tesseract`) | `apt install tesseract-ocr tesseract-ocr-eng` / runtime |
| Apple Vision | - | built in (`pyobjc-framework-Vision`) | - |
| Windows OCR | built in (`winrt-*` packages) | - | - |
| RapidOCR (ONNX) | Python <= 3.13 | Python <= 3.13 | Python <= 3.13 |
| Gemma / Claude vision | through the AI failover chain | same | same |

`python scripts/manage.py check` shows which engines are available on your machine.

## Development

### The management CLI

One cross-platform entry point - always run it with the project interpreter (`.venv/bin/python` on macOS/Linux,
`.venv\Scripts\python` on Windows):

```bash
python scripts/manage.py --help
```

| Command | What it does |
|---|---|
| `init-db [--with-test-db] [--wait 120] [--check]` | creates the database if missing (via `DATABASE_ADMIN_URL`, or the app URL against the `postgres` database), the app role, `CREATE EXTENSION vector, citext, pg_trgm` (superuser; prints OS-specific pgvector install hints on failure), then `app.db.create_all()` |
| `reset-db --yes` | drops and recreates every table (**deletes all data**; refuses in production without `--force`) |
| `seed` | loads the demo data into the database (`scripts/seed_demo.py` <- `scripts/demo/dataset.json`) |
| `create-superadmin [--email E] [--reset-password]` | creates a platform super-admin or resets its password (prompted, or `--generate-password`) |
| `unban <ip> [--note N]` | lifts every ban of a network address from the server's terminal (e.g. after failed platform-admin sign-ins) |
| `build-assets [--no-download] [--js-only]` | Tailwind CSS + JS bundle into `app/static/dist` |
| `gen-videos [--only push-up] [--quick] [--list]` | generates the pre-recorded workout videos (`scripts/generate_videos.py` -> `app/static/videos`) |
| `check [--json] [--no-network] [--strict]` | diagnostics table, exit code 1 on critical failures (also `python scripts/check_env.py`) |
| `run [--reload] [--workers N] [--host H] [--port P]` | starts the app (`python -m app`, or uvicorn workers) |
| `test [pytest args]` | runs pytest |
| `stress [args]` | runs `scripts/stress_test.py` |
| `make-env [--set KEY=VALUE]` | creates/completes `.env` with fresh secrets (never overwrites real values) |
| `configure --db docker\|runtime\|native\|existing` | writes the connection settings of a setup mode into `.env` |
| `env get\|ensure\|set` | reads or writes single `.env` values (used by the setup scripts) |
| `ai-status` | which AI providers answer right now (gateway models + status page, Docker, Claude, Kokoro) |
| `ai-test [--provider NAME]` | one tiny real answer from each AI provider (or one) through the app's own code |
| `ai-fallback` | starts the local AI fallback (Ollama in Docker, or a native Ollama) in the background - `run.sh` does |
| `ai-chat ["question"]` | chat with Gemma from the terminal through the OpenAI SDK (see [Talk to Gemma from the terminal](#talk-to-gemma-from-the-terminal)) |

`check` covers: Python version, active venv, key packages, `.env` + required secrets, PostgreSQL connectivity,
extensions and pgvector version, Redis, Tesseract + languages, OCR engines, Tailwind CLI + built assets, ffmpeg,
generated videos, AI providers (gateway `/models` + status page, Docker `/models`, Anthropic key), Kokoro, SMTP,
Geoapify, Cashfree mode, the production settings and whether the app port is free.

### Demo data

`python scripts/manage.py seed` (run by the setup scripts) loads a clearly fictional dataset **into the database**:
8 gyms (Bengaluru/Mysuru, Mumbai, Pune, Delhi, Hyderabad, Chennai, Dubai in AED and an online-only gym) with verified
and draft branches, about 70 owners, trainers, desk staff and members (including minors with guardians and a
suspended member), plans, subscriptions, 21-day metric histories, chats, jobs, misconduct reports, payroll, and a
shop per gym with 7-13 products, coupons, orders, refunds and reviews. All images are synthetic (DEMO / SPECIMEN).

Nothing about the demo data is hardcoded in the app. It is *data* - `scripts/demo/dataset.json` - that
`scripts/seed_demo.py` writes to PostgreSQL through the app's own models and services. After loading, the demo gyms,
accounts and shops are ordinary rows: every page reads them from the database, and owners can edit, hide or delete
them like anything else (`tests/test_demo_data.py` checks that `app/` contains none of it). The loader is idempotent
and additive: re-running it after editing `dataset.json` adds only what is missing. Load part of it with
`python scripts/seed_demo.py --gym forge-and-flow --gym desert-strength`.

The demo accounts and their **randomly generated password** are written to **`var/demo_credentials.txt`**
(mode 600) - passwords are never hard-coded. The super-admin uses `SUPERADMIN_EMAIL` / `SUPERADMIN_PASSWORD` from
`.env` and signs in at `/superadmin/login`; create more or reset one with `python scripts/manage.py create-superadmin`.

### Development workflow

- `./run.sh --reload` / `run.ps1 -Reload` / `RELOAD=true` - restart on code changes.
- **Asset watcher:** `python scripts/dev_watch.py` runs the Tailwind CLI in watch mode and re-bundles the JS when a
  file in `app/static/src/js/` changes. One-off build: `python scripts/manage.py build-assets`.
- **Additive auto-migrate on startup:** the app creates missing tables and then adds missing columns, indexes,
  unique/CHECK constraints and widened enum checks to existing tables (`AUTO_MIGRATE=true`). It never drops tables,
  columns or data. Run the same thing by hand with `python scripts/manage.py init-db`.
- Coding rules (async everywhere, no N+1, strict CSP, CSRF on every POST, PRG redirects, notifications, audit) are
  in [ARCHITECTURE.md](ARCHITECTURE.md).

### Project structure

```
app/
  main.py, __main__.py   app factory (middleware, routers) / `python -m app` launcher
  config.py, db.py       settings from .env / async engine, sessions, additive schema sync, QueryCounter
  models/                every table (users, gyms, billing, staff, health, engagement, shop, live, system)
  core/                  templating, auth guards, errors, navigation, icons, geo helpers, caps, pagination
  security/              CSP/headers, CSRF, security gate, scanner, rate limits, bans, sessions, passwords, crypto
  ai/                    provider router (gateway -> Docker -> Claude) + guard, queue, memory + per-feature AI modules
  services/              business logic (jobs, notifications, payroll, health, shop, OCR, checkout, live coach ...)
  routers/               one module per area (public, auth, account, owner_*, checkout, coach, chat, shop ...)
  templates/             Jinja2 pages (base.html + _ui.html macros)
  static/                src/js (vanilla JS components), dist (built CSS/JS), vendor, videos
docker/                  compose.yml, compose.gpu.yml, kokoro.compose.yml, Dockerfile, healthcheck.py
docs/                    ai-guard-and-queue.md (AI guard + queue contract), load-test-page.md, readme/ (README assets)
loadtest/                the 10-minute Locust stress-test kit (own database, Redis db 9, port 12000)
scripts/                 manage.py (CLI), init_db.py, check_env.py, make_env.py, build_assets.py, dev_watch.py,
                         seed_demo.py, stress_test.py, lan.py, find_query_loops.py, runtime_postgres.sh,
                         runtime_redis.sh, windows/*.ps1
tailwind/                input.css (design system) + per-area CSS
tests/                   pytest suite (conftest fixtures, factories, test_*.py)
setup.sh, run.sh         macOS / Linux setup and start
setup.ps1, run.ps1       Windows setup and start
ARCHITECTURE.md          engineering contract       TERMS.md   terms & conditions + disclaimer
```

## Testing

**3,792 tests pass (2 skipped; 3,794 collected).** They run against a real PostgreSQL + pgvector database named in
`TEST_DATABASE_URL` (the name must contain "test"); tables are created once per session and truncated after every
test, AI providers are faked. Parallel work packages each use their own database (`ironpulse_test_1`,
`ironpulse_test_2`, ...) - never the app's main database:

```bash
python scripts/manage.py init-db --with-test-db            # creates TEST_DATABASE_URL's database + extensions
python scripts/manage.py test -q                            # = python -m pytest -q
TEST_DATABASE_URL=postgresql+asyncpg://ironpulse:PASS@localhost:5432/ironpulse_test_3 \
  python -m pytest tests/test_scripts.py -q                # one file against its own database
```

On Windows set the variable first: `$env:TEST_DATABASE_URL = "postgresql+asyncpg://..."`.

Beyond the features themselves, the suite guards:

- **No N+1 queries.** `tests/test_no_n_plus_one.py` calls every GET page and GET JSON API as every role (anonymous,
  member, staff/trainer, owner, super-admin) on a small world, grows every collection (25 more rows by default) and
  calls them again: a route fails when it runs more statements, or any statement at least twice as often, on the large
  world. POST routes that act on many rows are measured with 2 and with 20 rows, background work in
  `tests/test_no_n_plus_one_jobs.py`. A loop scan (`scripts/find_query_loops.py`) lists every database call made once
  per iteration of a loop in `app/**`: each is fixed or allowlisted with its reason in `tests/n_plus_one_allowlist.json`.
- **Docs that match the code.** `tests/test_limits_docs.py` and `tests/test_caps.py` read every value in
  [Limits](#limits) from the code; other tests check this README's setup, AI, production and disclaimer sections.
- **Every bug-hunt finding** with a regression test (below).

### Bug hunt (November 2026)

Eleven bug hunters attacked the running app in parallel (no load tests this round): cross-account leaks and AI quality;
caps and sign-in; the public site and a shop "storm" (20-200 buyers at once on 4 processes); chat and safety; owner and
admin; web security; trust and safety; and the platform itself. Every finding was reproduced by an independent verifier
before it was fixed, and each fix has a regression test that failed on the old code.

| Severity | Confirmed | Examples (all fixed) |
|---|---|---|
| Critical | 3 | any owner could enrol another gym's member at the cash desk and then read and change their health record; the desk form revealed a stranger's name, other gym, ID status and safety pause; other gyms' trust & safety actions showed in an owner's audit log |
| High | 17 | image "decompression bombs" (a 380 KB PNG taking 1 GB); a NUL-byte request erasing other attackers' bans; any web page could get a visitor banned; suspended staff still reading members' chats and safety panels; the owner's health save splitting, renaming or deleting the member's own entries; unbounded health alerts, AI safety reviews and staff chat messages; checkout charging racers for units it then refunded; parallel reviews giving HTTP 500; a banned person's job reactivated; invitations accepted after a violation; the shop AI ignoring saved allergies it could not map |
| Medium | 15 | a copied cookie surviving a password change; races in invite throttles and misconduct reports; warnings missing "don't forget your box jumps" and typos; staff probing a member's conditions through the warning; a red flag in a closed chat answered with an error; the chat poll skipping a late message; prompt-injection text in look-alike characters; checkout placing an order at a total the buyer never saw; LAN mode refusing a restart; ids out of the 64-bit range giving HTTP 500 |
| Low | 9 | a stale sign-in tab giving a CSRF error; invisible or bidi-only chat messages; job-enquiry spam with non-breaking spaces; "already being prepared" for a refunded order; missing security headers on the 400 / 308 answers; public-suffix host wildcards accepted in production |

**44 confirmed findings, all fixed.** 8 more reports were judged intended, documented behaviour (e.g. the red-flag
exemption from the chat cap, review names on product pages, the 30-orders-a-day cap counting unpaid orders) and left as
they are. The fixes are described in the sections above; every new limit is in [Limits](#limits).

### Stress and load tests

`python scripts/manage.py stress [options]` runs `scripts/stress_test.py` (asyncio + httpx) against a running server;
every option is passed through:

```bash
python scripts/manage.py stress --base-url http://127.0.0.1:10000 --scenario mixed --concurrency 20 --requests 1000
python scripts/manage.py stress --scenario public --duration 30 --max-p95-ms 800
python scripts/manage.py stress --scenario security        # checks rate limits + attack blocking (see below)
```

Scenarios: `public` (anonymous pages), `api` (JSON endpoints), `auth` (sign-in page + wrong passwords), `security`
(must hit the rate limit and get attack probes blocked) and `mixed`. Exit code 0 = pass, 1 = 5xx / too many errors /
p95 too slow / a failed security check. Add your IP to `RATE_LIMIT_WHITELIST` for heavy load tests, otherwise the
rate limiter (correctly) answers 429. The `security` scenario deliberately trips the security gate, so the calling
IP is **banned** afterwards (lift it in `/superadmin/bans`) unless it is whitelisted.

The production-like **10-minute stress test** (Locust, `ENVIRONMENT=production`, its own database, Redis db 9 and
server on port 12000 - never the app's main database) is the kit in [loadtest/README.md](loadtest/README.md); its
`summary.json` feeds the super-admin page `/superadmin/load-test` ([docs/load-test-page.md](docs/load-test-page.md)).
Results: [Load test results](#load-test-results).

## Deploying to production

> ⚠️ Whoever deploys IronPulse is responsible for its legal compliance (privacy and data-protection law, payments,
> health data, consent) - get legal review before production use. See [Disclaimer & no liability](#disclaimer--no-liability).

`ENVIRONMENT=production` is **secure by default**. Unless the environment (or `.env`) sets them explicitly:

| Setting | In production | Effect |
|---|---|---|
| `COOKIE_SECURE` | `true` | session and CSRF cookies are `Secure` with the `__Host-` prefix; HSTS header |
| `FORCE_HTTPS` | `true` | plain HTTP is redirected (308) to HTTPS; HSTS, `upgrade-insecure-requests` |
| `TRUST_PROXY_HEADERS` | `true` | `X-Forwarded-For` / `-Proto` are believed **only** from `TRUSTED_PROXIES` (default `127.0.0.1,::1`) |
| `DEBUG`, `RELOAD` | always `false` | no API docs, no debug pages, no auto-reload |

An explicit `false` is honoured but logged as a WARNING at every start of every worker (only for a site without HTTPS,
e.g. an intranet trial).

**The app refuses to start** - one line per problem naming the variable to set, exit status 3, no traceback (a
multi-worker uvicorn stops instead of restarting its workers) - when:

- `SECRET_KEY` or `ID_HASH_PEPPER` is empty, too short, the `.env.example` placeholder or not random, or
  `DATA_ENCRYPTION_KEY` is not a Fernet key (`python scripts/make_env.py` generates all three);
- `ALLOWED_HOSTS` is empty, `*`, has no entry a request can match (`https://...`, `name:port` - the Host check
  compares bare names), or only localhost while `HOST` is not a loopback address (a wildcard other than
  `*.example.com` stops every environment: it would otherwise turn every request into a 500), or holds a wildcard
  for a public suffix - a top-level domain (`*.com`), a country second level (`*.co.in`, `*.com.au`) or a
  dynamic-DNS service (`*.duckdns.org`, `*.nip.io`): anyone could register a name there and point it at the server
  (DNS rebinding); name your own domain (`*.gym.example.com`);
- `CORS_ORIGINS` contains `*`, `null` or a plain `http://` origin other than localhost while `COOKIE_SECURE` is on
  (anyone on that page's network path could rewrite it and send signed-in requests), or `TRUSTED_PROXIES` contains
  a catch-all - `*` (uvicorn's "trust every client"), wider than an IPv4 `/8` or an IPv6 `/16` (`0.0.0.0/0`,
  `0.0.0.0/1`, `::/0`), or the IPv4-mapped `::ffff:0:0/96` (private IPv6 ranges - `fc00::/7`, `fe80::/10` - are fine) -
  or an entry that is not an IP address or network; or `FORWARDED_ALLOW_IPS` (read by uvicorn's own CLI, often `*` in
  container images) is a catch-all;
- `PUBLIC_BASE_URL` is `https://` but `COOKIE_SECURE=false` or `FORCE_HTTPS=false` (a stale `.env`), or
  `PUBLIC_BASE_URL` is plain `http://` (not localhost) while `COOKIE_SECURE` is on - a plain-HTTP intranet trial sets
  `COOKIE_SECURE=false` and `FORCE_HTTPS=false` explicitly;
- `SUPERADMIN_PASSWORD` is set but weak (shorter than 10 characters, well known or not random);
- `AI_PROVIDER_ORDER` names `simulated` without `AI_SIMULATED_ALLOWED=true`, or next to a real provider (production
  runs it only alone, for a load test).

```
[ironpulse] configuration error: ALLOWED_HOSTS allows only localhost (the development default) but HOST=0.0.0.0 accepts connections from the network - set ALLOWED_HOSTS to the site's host name(s), e.g. ALLOWED_HOSTS=gym.example.com
```

It starts but warns about: the explicit opt-outs above, `ALLOWED_HOSTS` left unset (only `localhost` is answered) or
holding an entry that never matches (`https://...`, `name:port` - the Host check compares bare names), and a
`PUBLIC_BASE_URL` that points at localhost or names a host `ALLOWED_HOSTS` does not accept (those links would get
400), and a plain `http://` `CORS_ORIGINS` entry on a site that set `COOKIE_SECURE=false`. A `.env` made from an older
`.env.example` pins `COOKIE_SECURE=false`, `FORCE_HTTPS=false`, `TRUST_PROXY_HEADERS=false` and
`ALLOWED_HOSTS=localhost,...`: delete those lines (or set the real values) before switching to production.

A production `.env` behind a TLS-terminating reverse proxy on the same machine:

```dotenv
ENVIRONMENT=production
HOST=127.0.0.1                       # the address uvicorn binds (the proxy forwards to it)
PORT=10000
PUBLIC_BASE_URL=https://gym.example.com
ALLOWED_HOSTS=gym.example.com
TRUSTED_PROXIES=127.0.0.1,::1        # your proxy's address(es) as the app sees them
REDIS_URL=redis://:PASSWORD@127.0.0.1:6379/0
SEED_DEMO_DATA=false
# SECRET_KEY, DATA_ENCRYPTION_KEY, ID_HASH_PEPPER, SUPERADMIN_PASSWORD: python scripts/make_env.py
```

```nginx
location / {
    proxy_pass http://127.0.0.1:10000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;                   # WebSocket /ws
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Then `python scripts/manage.py check` (the **Production settings** row lists every warning) and
`python scripts/manage.py run --workers 4` (sets `REALTIME_PG_BRIDGE=true`; several workers need `REDIS_URL`).

- `HOST` is what the localhost-only check reads. `python -m app`, `manage.py run` and the Docker image bind it; when you
  start `uvicorn app.main:app --host ...` yourself, set `HOST` to the same address, and pass
  `--forwarded-allow-ips` with your `TRUSTED_PROXIES` addresses (never `*`; otherwise uvicorn trusts whatever
  `$FORWARDED_ALLOW_IPS` says) - `manage.py run --workers N` does both for you.
- A proxy on another machine or a Docker bridge: add its address or range to `TRUSTED_PROXIES` (e.g. `172.16.0.0/12`),
  never `0.0.0.0/0` - otherwise every visitor shares the proxy's address (one rate limit, one ban for all).
- Health checks: with `FORCE_HTTPS` a plain-HTTP probe of `/healthz` gets a 308. Probe through the proxy, or send
  `X-Forwarded-Proto: https` from a `TRUSTED_PROXIES` address (as `loadtest/run_server.sh` does); the probe's `Host`
  must be in `ALLOWED_HOSTS` (add `127.0.0.1` for a local probe). The Docker image's own `HEALTHCHECK`
  (`docker/healthcheck.py`) does exactly that from `127.0.0.1` with the first `ALLOWED_HOSTS` name: keep `127.0.0.1` in
  `TRUSTED_PROXIES`.

### Load tests with simulated AI

The `simulated` AI provider answers locally with realistic latency and no model, so a load test exercises the real AI
guard (quotas, duplicates, cooldowns), the fair queue (lanes, per-user caps, `max_wait`), provider slots, timeouts,
the circuit breaker and cancellation without a GPU. It is used **only** when named in `AI_PROVIDER_ORDER`
(`AI_PROVIDER_ORDER=simulated`) - never by default. Under `ENVIRONMENT=production` it also needs
`AI_SIMULATED_ALLOWED=true` and runs only **alone**: next to a real provider it would answer members with generated
text whenever the model fails, so the AI router leaves it out then. Every start of every worker logs a loud
`SIMULATED AI PROVIDER ACTIVE` warning. The super-admin AI page and its provider test show it as "Simulated AI (load
test)" (provider `simulated`).

| Setting | Default | Meaning |
|---|---|---|
| `AI_SIM_FIRST_TOKEN_MS` | `400..1500` | time to the first words, uniform (one number = fixed) |
| `AI_SIM_TOKENS_PER_S` | `25..60` | words per second, one rate per call (each streamed chunk +-25 %) |
| `AI_SIM_FAILURE_RATE` | `0` | share of calls (0..1) that fail like an unreachable server - the circuit breaker counts them |
| `AI_SIM_MAX_CONCURRENCY` | `4` | provider slots, like `AI_GATEWAY_MAX_CONCURRENCY` |
| `AI_SIM_TTS`, `AI_SIM_TTS_MS` | `true`, `300..1200` | the coach voice (Kokoro lane) is simulated too: silent MP3 after that delay |
| `AI_SIMULATED_ALLOWED` | `false` | required with `ENVIRONMENT=production` |
| `AI_SIM_ECHO_NONCE` | off | bug hunts: every answer starts with `[nq…]` - the nonce tokens (`nq` + 8 of a-z0-9, e.g. `nq7k2m9x4a`) in the requester's own words (the question the feature passed as `user_text`, else the prompt) - so a test can prove an answer belongs to its question and its user |
| `AI_SIM_CAPTURE_PROMPTS` | empty | bug hunts: a JSONL file every prompt is appended to - `system`, `prompt`, `user_id`, `feature`, `user_text`, `kind`, `schema` - within 0.2 s, mode 0600, at most `AI_SIM_CAPTURE_MAX_MB` (256) MB: what context reached the AI, and that no other member's data did |

Answers are deterministic per prompt (plausible coaching sentences; structured calls get schema-valid JSON with
conservative values - no actions, approvals or alarms). Remove `simulated` from `AI_PROVIDER_ORDER` after the test.
`AI_SIM_ECHO_NONCE` and `AI_SIM_CAPTURE_PROMPTS` can be set in `.env` or as environment variables of the server
process, act only on the simulated provider, and are ignored under `ENVIRONMENT=production`.


## Troubleshooting

| Problem | Fix |
|---|---|
| First stop: what is wrong? | `python scripts/manage.py check` - every failing line comes with a fix |
| `extension "vector" is not available` | pgvector is missing in your PostgreSQL: Docker (`--db docker`), runtime (`./setup.sh --db runtime`), `brew install pgvector`, `apt install postgresql-17-pgvector`, or on Windows `scripts\windows\install_pgvector.ps1` (Administrator) |
| `permission denied to create extension` | set `DATABASE_ADMIN_URL` to a superuser URL and run `python scripts/manage.py init-db` |
| `password authentication failed` | the password in `DATABASE_URL` does not match the role; with `DATABASE_ADMIN_URL` set, `init-db` re-syncs it |
| Cannot reach PostgreSQL | runtime: `scripts/runtime_postgres.sh start`; Docker: `docker compose --env-file .env -f docker/compose.yml up -d db`; Windows service: `Start-Service postgresql-x64-17` |
| "AI unavailable" banner | `python scripts/manage.py ai-status`; start the gateway, allow its port in the firewall, or start the Docker failsafe |
| Unstyled pages | `python scripts/manage.py build-assets` (or keep `scripts/dev_watch.py` running) |
| Port already in use | another program (or another IronPulse) uses `PORT` - change `PORT` and `PUBLIC_BASE_URL` in `.env` |
| LAN mode refuses to start | the development server already uses that port - stop it, or `./run.sh --lan --port 11001` |
| Windows: "running scripts is disabled" | start scripts with `powershell -ExecutionPolicy Bypass -File setup.ps1` |
| Windows: Docker does not start | open Docker Desktop once, accept its terms, enable WSL 2, restart Windows, run `setup.ps1` again |
| Linux: `permission denied ... docker.sock` | `sudo usermod -aG docker "$USER"`, log out and back in |
| Redis warnings in the log | Redis is optional - start it (`scripts/runtime_redis.sh start` / Docker) or clear `REDIS_URL` |
| No voice in the live coach | Kokoro is not running - the browser voice is used; start it with the `voice` profile |
| Tests refuse to run | `TEST_DATABASE_URL` must name a database containing "test"; create it with `init-db --with-test-db` |
| `ModuleNotFoundError` | use the venv interpreter (`.venv/bin/python` / `.venv\Scripts\python`) or `run.sh` / `run.ps1` |

## Disclaimer & no liability

**The owner, the developer and the AI are NOT responsible for anything; this is just a student-grade project;
nothing here is medical, legal or financial advice.**

- **Provided "AS IS".** IronPulse is a student-grade project, provided "as is", without warranty of any kind.
- **Not advice, not a professional.** Nothing in it - health metrics, AI answers, coach plans, live coaching, trainer
  safety warnings, red-flag alerts, product safety notes - is medical, legal or financial advice or a substitute for a
  qualified professional (doctor, physiotherapist, dietitian, lawyer, accountant). IronPulse does not measure heart
  rate, BPM or blood pressure, and it is not a medical device.
- **AI can be wrong.** AI output can be wrong; double-check important information.
- **Safety features are aids.** Health checks, safety pauses, trainer warnings, red-flag alerts and allergen or
  product safety notes can miss things - they are not a guarantee.
- **Estimates, not guarantees.** Payroll, tax and gratuity figures are estimates; verification badges are not
  guarantees; supplements are not medicines. Payments run in a clearly-labelled test mode unless you add your own
  gateway keys.
- **No liability.** To the fullest extent permitted by law, the authors and contributors accept no liability for any
  injury, health problem, data loss, security incident, financial loss or other damage arising from using, deploying
  or modifying IronPulse.
- **Deployers are responsible.** Whoever deploys IronPulse is responsible for their own legal compliance - privacy and
  data-protection law, payments, health data and consent - and should get legal review before production use.
- **Load-test numbers** come from one laptop and are not a performance guarantee.
- **In an emergency**, call your local emergency number (112 in India and the EU, 911 in the US, 999 in the UK) - never
  rely on this app.

People who use a running IronPulse are covered by its terms, [TERMS.md](TERMS.md) (shown in the app at `/terms`), which
say the same in ["Disclaimer - please read"](TERMS.md#disclaimer---please-read) and
["18. Limitation of liability"](TERMS.md#18-limitation-of-liability).

## License

The code is released under the [MIT License](LICENSE).

MIT's "AS IS" clause - no warranty, no liability - covers the code and everyone who copies, modifies or deploys it.
People who use a running IronPulse are covered by the app's terms instead: [TERMS.md](TERMS.md) ("Disclaimer - please
read" and "18. Limitation of liability"). The license does not replace the [Disclaimer & no liability](#disclaimer--no-liability)
above: IronPulse is still not medical advice, AI can still be wrong, and the safety features are still only aids.
