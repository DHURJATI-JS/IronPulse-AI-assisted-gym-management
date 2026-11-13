# IronPulse load-test kit (Locust, production mode)

A production-like **10-minute stress test** of IronPulse: its own database, Redis db and server; realistic browsers
(per-page WebSockets, visitor churn, browser cache, CSRF, one-time submit ids, checkout, simulated AI); **random junk**
in the seeded data and from ~10 % of the users (section 6a); a step + spike + recovery load shape; resource
monitoring; and a verdict against production SLOs in `report.md` plus `summary.json`. The super-admin page
`/superadmin/load-test` reads `summary.json` (docs/load-test-page.md).

**One session per account.** The app signs an account out everywhere else on every sign-in
(`SINGLE_SESSION_PER_ACCOUNT`, default on). Every simulated user therefore signs in with an account of its own -
members, owners, staff, the super-admins (`lt_superadmin_01..10`), the junk testers (`lt_junk_*`) and the abusers -
and "signed in elsewhere" on a user's own session is a **failure** (it means two simulated users share an account).

**Always re-seed before a run** (section 2): the app adds tables and columns often, and the startup schema sync of
a stale database could lock tables while the server starts.

Everything lives in `loadtest/`, plus the Locust virtualenv `.venv-loadtest/` at the project root. The kit
**never** touches the app's main database, the dev servers on ports 10000 and 11000, Redis db 0, `.env` or `var/`:

| resource | load test | dev |
|---|---|---|
| database | `ironpulse_loadtest` (own DB, `CONNECTION LIMIT 70`, `jit=off`, `random_page_cost=1.1` for this DB only) | `ironpulse`, `ironpulse_test*` |
| Redis | logical db **9** (only `FLUSHDB` on 9, never `FLUSHALL`) | db 0 |
| server | `127.0.0.1:12000`, 4 uvicorn workers, `ENVIRONMENT=production` | 10000, 11000 |
| secrets | `loadtest/.secrets.env` (generated, 0600, gitignored) | `.env` |
| files | `loadtest/var/` (uploads, spooled e-mail) | `var/` |
| outbound calls | none: no SMTP, geocoding, map tiles, payments, paid or LAN AI (by default) | - |

Run every command from the project folder, and quote its path if it contains spaces (`cd "/path/to/my project "`).

## 1. Setup (once)

```bash
loadtest/setup_venv.sh            # -> .venv-loadtest with locust 2.47.0 + websocket-client 1.9.2
```

What worked here: the project's own **Python 3.15.2** (`/Library/Frameworks/Python.framework/Versions/3.15`) with
`pip install --only-binary=:all:`. gevent 26.9.0, greenlet 3.5.6, geventhttpclient 2.6.1, zope.interface and psutil
7.2.2 all ship cp315 or abi3 wheels for macOS arm64, so nothing is compiled. If a future Python lacks wheels, run
`loadtest/setup_venv.sh --conda`: it uses micromamba from `~/.ironpulse/bin` to build `loadtest/.env-conda` with
Python 3.12 from conda-forge and puts the venv on top of it. The system Python 3.9 is too old.

## 2. Seed `ironpulse_loadtest` (~2 min)

```bash
.venv/bin/python loadtest/seed_loadtest.py                  # full data set + 10 % random junk (idempotent rebuild)
.venv/bin/python loadtest/seed_loadtest.py --junk-share 0.2 # more junk in the data (0 .. 1; --no-junk = none)
.venv/bin/python loadtest/seed_loadtest.py --reset-runtime  # keep the data, remove what a run added (see below)
```

The seeder runs with the **project** interpreter because it imports the app. It builds:

* a fresh, empty `ironpulse_loadtest` owned by the app role from `DATABASE_URL`, using the superuser from
  `~/.ironpulse/pg_superuser_password`. Extensions `vector`, `citext` and `pg_trgm` are created the way
  `scripts/init_db.py` does it. An existing database gets its schema dropped instead of `DROP DATABASE`, which forces
  a checkpoint of the whole shared cluster; `--drop-database` does the literal drop.
* the schema through the app's own `create_all()` plus `sync_schema`, `bootstrap()`, and the demo dataset through
  `seed_demo(..., write_credentials=False)`. `var/demo_credentials.txt` is not touched, and the demo accounts move to
  `@demo.ironpulse.test`.
* bulk data with CSV `COPY` (no ORM objects). Fixed random seeds make every rebuild identical, junk included.
* **random junk** in 10 % of the seeded display texts (`--junk-share`, section 6a), from its own random generator.
* `products.rating_avg` / `review_count` **backfilled** right after the COPY by one set-based `UPDATE` from the
  visible reviews (the app's own expression, `round(avg(rating), 4)`; `updated_at` untouched). COPY leaves them
  NULL / 0, and the app's hourly reconcile (`backfill_product_ratings`, first run 60 s after start) would otherwise
  rewrite thousands of products during the run. The seeder checks that no product is left out of line.
* the **remembered accounts** of the sign-in page's account chooser for 30 % of the members (positions 0, 3, 6 of
  every ten in `accounts.json`): the opaque reference and the browser's device key go to `accounts.json`
  (`rr` / `rd`), the database keeps only their SHA-256, like `app/services/remembered_accounts.py`.

The seeder refuses to run while something listens on :12000.

**Data** (build of 2026-11-13: 120 s, 1,144 MB, on a busy machine):

* **Directory:** 308 gyms, Pareto-sized chains up to about 220 branches. 2,016 branches around 25 Indian cities,
  with Bengaluru (12.97, 77.59) the heaviest.
* **People:** about 22,860 users: 20,000 `lt_member_*`, 200 `lt_owner_*`, 2,000 `lt_staff_*`, 80 `lt_abuser_*`,
  10 super-admins `lt_superadmin_01..10` (one per simulated admin), 500 junk testers `lt_junk_001..500` (members
  with an online membership of one gym and nothing else, for the signed-in JunkUsers), the bootstrap
  `lt_superadmin` (the server's `SUPERADMIN_EMAIL`, used by no simulated user) and the demo users. 2,026 employees.
* **Members' data:** 26,910 subscriptions, 493k health readings, 181k tasks.
* **Shop:** 20,065 products with local pgvector embeddings, 20,020 orders with 33k items, 43k wishlist items.
* **Chat and notifications:** 7.4k chat threads with 113k messages, 203k notifications.
* **Jobs:** 2,013 job postings with 1.9k enquiries.
* **Money and audit:** 50k audit-log rows, 100k transactions.

It also seeds **production history** for the super-admin pages and the background jobs: 150k security events over
90 days (40 % not yet AI-reviewed), 5k IP bans (148 active), 60k AI call logs, 150k outbox e-mails, 30k finished
jobs, 20k sessions and 156k assistant messages.

A **heavy tail** goes first in `accounts.json`, so every load step uses it:

* the owners of the 3 largest chains, with about 34k extra notifications and about 800 team threads each;
* 400 members with 200+ tasks and 50+ wishlist items;
* 4 trainers with 200 clients each.

Row counts and timings: `loadtest/seed_summary.json`.

**Outputs**, all gitignored:

* `accounts.json`: every account with its fixed client address and ids (`superadmins`, `junk` included; members
  with a remembered account carry `rr` / `rd`). It contains the shared test password.
* `catalogue.json`: gym slugs, branch ids, product ids, job ids, trainers, cities and search terms; `foreign`:
  other people's object ids (orders, threads, tasks, notifications, documents, staff, payroll ...) that no
  `lt_junk_*` account owns, for the IDOR probes, and each table's largest id.
* `seed_summary.json`: row counts, timings, the seeded id floors, and `junk` (share, seed, values per column,
  samples).
* `.secrets.env`: the server's own `SECRET_KEY`, `ID_HASH_PEPPER` and `DATA_ENCRYPTION_KEY`, used by seeder and
  server alike. Delete it only together with a re-seed.

`--reset-runtime` (a few seconds) deletes only rows **above the seeded id floors** of the runtime tables (bans, security
events, AI logs, jobs, outbox, notifications, chat and assistant messages, audit, metrics), sessions created after the
seed, and login lockouts. It also flushes Redis db 9 and removes the spooled mail, so the seeded history survives.
A run also places orders (stock), changes carts, uses up the remembered-account references (a sign-in issues new
ones) and lets the junk senders create threads, tasks, sign-ups and enquiries, so **re-seed before every real
run** - `--reset-runtime` is for quick experiments only.

## 3. Server: start, warm up, stop

```bash
loadtest/run_server.sh start --run NAME [--ai simulated|off|gateway] [--smtp spool|sink] [--workers 4] [--backlog N] [--force]
loadtest/run_server.sh warmup [330]    # wait until 330 s after the server became healthy
loadtest/run_server.sh status
loadtest/run_server.sh stop            # monitor + server + SMTP sink - only processes this script started
```

`start` does the following, in order:

1. **PID files.** It drops stale PID files: a process is signalled only when its PID, its start time **and** its
   command line match what `start` recorded (`ltlib/procs.py`, registry `.procs.json`). PID files left by a crash or
   a reboot, whose PIDs are gone or reused by unrelated programs, are reported and removed, never killed. The same
   check guards `stop`.
2. **Preflight.** It refuses when the shared machine is busy (`ltlib/preflight.py`): 1-minute load above 2, a pytest
   process, or more than 30 PostgreSQL connections of other databases. `--force` starts anyway and the report marks
   the run.
3. **Run folder.** It refuses a run folder that already holds files.
4. **Connection budget.** The budget is workers × (pool + overflow + 1 LISTEN) = 4 × (10 + 5 + 1) = **64**, the app's
   default pool. It must fit the database's CONNECTION LIMIT (70), 80 % of `max_connections - reserved` (the app's own
   rule, `app/db.py`) and what the other databases leave free right now. `IRONPULSE_WORKERS` and `WEB_CONCURRENCY` are
   both set to the worker count, so the app's startup budget check knows it.
5. **Environment.** It builds the environment in `ltenv.py`, using environment variables only, since they beat `.env`.
6. **Validation.** It validates the environment with the app's own loader (`build_settings`), because production mode
   refuses unsafe settings. It then writes the effective settings, with no secrets, plus the pinned values, the
   platform and the PostgreSQL settings to `<run>/server_config.json`.
7. **Server.** It starts `python -m uvicorn app.main:app --host 127.0.0.1 --port 12000 --workers 4 --no-server-header
   --proxy-headers --forwarded-allow-ips 127.0.0.1 --backlog 2048`, the same flags as `scripts/manage.py run`, with
   no reload, in its own session. The log goes to `<run>/server.log`.
8. **Monitor.** It waits for `/healthz` and all workers, prints the PIDs and starts `monitor.py`.

**Production settings** (`ltenv.server_env`):

* Mode and proxy: `ENVIRONMENT=production`, `DEBUG=false`, `RELOAD=false`, `PUBLIC_BASE_URL=https://127.0.0.1:12000`,
  `ALLOWED_HOSTS=127.0.0.1,localhost`, `CORS_ORIGINS` empty. `COOKIE_SECURE`, `FORCE_HTTPS` and `TRUST_PROXY_HEADERS`
  are set explicitly. `TRUSTED_PROXIES=127.0.0.1`, and `RATE_LIMIT_WHITELIST` is empty, so nobody is exempt.
* Own resources: `REALTIME_PG_BRIDGE=true`, `REDIS_URL` pointing at db 9, background jobs on.
* Secrets: the generated secrets above, and `SUPERADMIN_PASSWORD` = the test password (22 random-looking characters).

Every load-relevant setting is **pinned to the app's own defaults** from `app/config.py`, read at launch, so the
developer's `.env` never applies. For example, `.env` has `RATE_LIMIT_AI_PER_MINUTE=20` where the app default is 60.
The pinned settings cover rate limits, ban and lockout thresholds, the AI guard and queue, job concurrency, the session
TTL, the DB pool, timeouts and retention. To change one for an experiment, set `LT_<NAME>=value`; it is recorded.

**What `ENVIRONMENT=production` does now:**

* It turns on Secure `__Host-` cookies, the HTTPS redirect with HSTS and proxy trust, unless they are set explicitly.
* It forces debug and reload off.
* It **refuses to start** (exit status 3, one line per problem) on any of these:
  * a placeholder, short or non-random `SECRET_KEY` or `ID_HASH_PEPPER`;
  * a `DATA_ENCRYPTION_KEY` that is not a Fernet key;
  * `ALLOWED_HOSTS` that is empty, `*`, or localhost-only with a non-loopback `HOST`;
  * `CORS_ORIGINS=*` or `TRUSTED_PROXIES=0.0.0.0/0`;
  * a weak `SUPERADMIN_PASSWORD`;
  * simulated AI without `AI_SIMULATED_ALLOWED`.

The launcher's environment passes `build_settings` and `scripts/manage.py check` (0 failures; 1 warning, the expected
"Simulated AI").

**AI modes** (`--ai`; the mode is recorded in `server_config.json`, `run_meta.json` and `summary.json` `target.notes`):

| mode | what runs | when |
|---|---|---|
| `simulated` (**default**) | the app's load-test provider (`AI_PROVIDER_ORDER=simulated`, `AI_SIMULATED_ALLOWED=true`, gateway URLs empty): first words after 400-1500 ms, 25-60 words/s, 4 provider slots, simulated voice 300-1200 ms, 0 % failures. It goes through the real AI guard, fair queue, slots, timeouts and circuit breaker, and never calls a model. | the real run |
| `off` | no provider: deterministic fallbacks before the guard runs. AI rows are kept out of the SLOs and the report says "not exercised". | comparison |
| `gateway` | the LAN Gemma gateway from `.env` (a shared machine) | only with its owner's OK |

`LT_AI_SIM_FAILURE_RATE=0.02` and similar settings vary the simulated provider.

**Mail and other integrations** (`--smtp`): `spool` (default) writes mail to `loadtest/var/mail`, with no SMTP
transaction. `sink` starts `smtp_sink.py` on 127.0.0.1:2525, which accepts each mail, waits 100-300 ms and discards it,
so the real delivery path runs. Map tiles answer a local transparent PNG (no Geoapify key, `MAP_OSM_FALLBACK=false`),
and payments run in Cashfree TEST MODE. These stand-ins are cheaper than production, and the report lists them.

## 4. The 10-minute stress run: exact commands

Run it once the other teams are idle. `start` refuses a busy machine, and Locust refuses one for this profile too.

```bash
cd /path/to/ironpulse                                        # the project folder (quote it if it has spaces)
.venv-loadtest/bin/python loadtest/ltlib/preflight.py        # must end with "OK - the machine is quiet"
.venv/bin/python loadtest/seed_loadtest.py                    # fresh data (~2 min)
RUN="stress-$(date +%Y%m%d-%H%M%S)"
loadtest/run_server.sh start --run "$RUN"                     # production mode, AI simulated, mail spooled
loadtest/run_server.sh warmup                                 # 330 s: past every background job's initial delay
ulimit -n 10240
.venv-loadtest/bin/locust -f loadtest/locustfile.py --host http://127.0.0.1:12000 --headless --processes 4 \
  --csv "loadtest/results/$RUN/locust" --csv-full-history --html "loadtest/results/$RUN/locust_report.html" \
  --logfile "loadtest/results/$RUN/locust.log" --loglevel INFO --only-summary > "loadtest/results/$RUN/locust_console.txt" 2>&1
loadtest/run_server.sh stop
.venv-loadtest/bin/python loadtest/analyze.py "loadtest/results/$RUN"     # report.md + summary.json (SLOs at 200 users)
```

The run folder is `loadtest/results/<RUN>`: `RUN` must be new (letters, digits, `_`, `.`, `-`, at most 64), and
`stress-YYYYMMDD-HHMMSS` keeps the runs sorted on `/superadmin/load-test`. On a busy machine `start --force` and
`LT_FORCE=1` run anyway; the report then says INCONCLUSIVE.

`LT_PROFILE` defaults to `stress10m`. The shape drives the run and Locust stops by itself after 600 s. The user
counts below include the **JunkUser** class (~10 %: about 20 junk senders at the 200-user SLO step, 160 at the
spike), whose traffic is judged by its own SLO row and kept out of the normal users' SLOs.

| time | users | spawn rate | role |
|---|---:|---:|---|
| 0-1 min | 50 | 2/s | warm-up |
| 1-3 min | 100 | 5/s | |
| 3-5 min | **200** | 10/s | **SLO step** (105 s steady) |
| 5:00-6:10 | 350 | 15/s | about 50 % of the estimated saturation |
| 6:10-7:20 | 700 | 25/s | about 100 % |
| 7:20-8:20 | 1,050 | 30/s | about 150 % (pre-spike) |
| 8:20-8:50 | **1,600** | 50/s | spike: ×1.5 within ~11 s |
| 8:50-10:00 | 200 | 200/s | recovery at the SLO load |

**Why these numbers.** The two smokes of this kit measured 6.3-11 ms of worker CPU and 3-6 ms of PostgreSQL CPU per
normal request, and about 0.75 requests/s per user at the 2-8 s think time. Four workers therefore saturate near
360-630 requests/s, about **450-850 users** (`analyze.py` prints this estimate per run). Both smokes ran next to the
other teams' work, so the true capacity may be higher.

The upper steps bracket that estimate, so the knee falls inside the run. The SLO step stays at 200 users, 3-5 minutes
in. The spike is a real ×1.5 jump, and the last 70 s show whether latency, the DB pool and the sockets recover.

If a quiet-machine calibration says otherwise, move the steps. The calibration is a 3-minute ramp to 600 users:
`LT_PROFILE=calibrate`, analysed with `--slo-users 600`, which prints "≈ N users at the 10-minute run's think time".
Then set `LT_STEPS="60:50:2,180:100:5,300:200:10,..."`, a list of end second, users and spawn rate. Keep the
200-user step and use roughly 50 / 100 / 150 % of N, then ×1.5 for the spike.

**Load generator.** `--processes 4` drives this shape. Locust needs about 0.7 ms per request and stayed under 15 %
CPU per process in the smokes. The monitor records the busiest Locust process. A step where one process exceeds
80 % CPU, or where Locust warns about CPU, is flagged GENERATOR SATURATED. In that case rerun with `--processes 6`.

**macOS accept queue.** macOS caps uvicorn's `--backlog` at `kern.ipc.somaxconn=128`, where production Linux uses
4096. Near the knee an overflowing accept queue adds 1-3 s SYN retries. The monitor counts `listen queue overflow`
per step and the report flags it. With an admin's OK, `sudo sysctl -w kern.ipc.somaxconn=4096` plus
`start --backlog 4096` removes this effect.

**Background jobs.** The periodic jobs (low-stock digest, security triage, subscriptions, payroll, retention and
others) start 45-300 s after the server and run in every worker. `warmup` waits past those initial delays, and
`report.md` lists the jobs seen in each step. On fresh data the first ticks are heavy: in the 23:43 IST smoke the
bedtime reminders sent 897 notifications within 6 s of the start, and the low-stock digest notified 196 owners
90 s in. Bedtime reminders go out 30 minutes before each member's own bedtime, so evening runs (roughly 19:30 to
01:00 IST) carry that extra write load.

## 5. Other profiles (`LT_PROFILE`)

| profile | what | users / time |
|---|---|---|
| `stress10m` | the real run (above) | 50 → 1,600 → 200, 10 min, 2-8 s think time |
| `smoke50` | the real shape compressed: 10/20/30/40/50 users, then 10 (recovery); 1-4 s waits; the first user of each class in each process runs every task once | ≤ 50, 3 min |
| `smoke` | fixed 2 visitors, 3 members, 1 owner, 1 staff, 2 super-admins (own accounts), 1 abuser, 4 junk users (2 anonymous, 2 signed in); 0.5-1.5 s waits; every user runs every task once first | 14, `LT_SMOKE_SECONDS` (120) |
| `calibrate` | ramp to 600 users at 5/s, then hold (CPU per request → saturation estimate) | 600, 3 min |
| `soak` | 30 min at 200 users: the **memory-leak** check (per-worker USS slope over the last 20 min) | 200, 30 min |
| any + `LT_STEPS` | custom steps `end_s:users:rate,...` | - |

Smoke, the exact commands (2 minutes, every class including the JunkUser, 14 users):

```bash
cd /path/to/ironpulse                                        # the project folder (quote it if it has spaces)
.venv/bin/python loadtest/seed_loadtest.py
RUN="smoke-$(date +%Y%m%d-%H%M%S)"
loadtest/run_server.sh start --run "$RUN" --force
ulimit -n 10240
LT_PROFILE=smoke LT_FORCE=1 .venv-loadtest/bin/locust -f loadtest/locustfile.py --host http://127.0.0.1:12000 --headless \
  --processes 2 --csv "loadtest/results/$RUN/locust" --csv-full-history --html "loadtest/results/$RUN/locust_report.html" \
  --logfile "loadtest/results/$RUN/locust.log" --loglevel INFO --only-summary > "loadtest/results/$RUN/locust_console.txt" 2>&1
loadtest/run_server.sh stop
.venv-loadtest/bin/python loadtest/analyze.py "loadtest/results/$RUN" --slo-users 14
```

The compressed shape: `LT_PROFILE=smoke50` with the same flags, then `analyze.py <run> --slo-users 50`.

Other settings:

* `LT_WS_MODE`: `per-page` (default, like the browser), `hold` (one socket per user, comparison only) or `off`.
* `LT_WAIT_MIN` / `LT_WAIT_MAX`: think time.
* `LT_AI_MIN_INTERVAL`: at most one AI ask per member every N to 1.5×N seconds (90 s by default).
* `LT_SUPERADMINS`: number of super-admin users (3 by default, at most 10: one seeded account each).
* `LT_JUNK_WEIGHT`: weight of the JunkUser class (22 by default, ~10 % of the users; 0 = no junk senders).
* `LT_JUNK_SIGNED_IN_SHARE`: share of the junk senders that sign in with their own `lt_junk_*` account (0.5).
* `LT_JUNK_AI_INTERVAL`: at least this many seconds between a junk sender's well-formed AI asks (150; 45 in the
  smoke profiles). The AI slots are shared with the members; malformed asks are refused before the AI anyway.
* `LT_VISITOR_SESSION_VIEWS`: page views per visit (6 by default).
* `LT_VISITOR_NEW_SHARE`: share of new visitors (0.7 by default).
* `LT_ABUSE_FRESH_IP_SHARE`: share of abuse bursts from a fresh botnet address (0.5 by default).
* `LT_FORCE=1`: run on a busy machine (marked in the report).

## 6. What the users do

Every simulated user is a browser behind the TLS proxy that Locust plays.

* **Addresses.** Each user has its own `X-Forwarded-For` from 198.18.0.0/15. Accounts have a fixed address each,
  visitors draw from 198.19.0.0/17, and abusers use their own block, 198.19.240.0/20. A ban of an abuser therefore
  never hits a normal user.
* **Headers.** `X-Forwarded-Proto: https`, a real User-Agent, and `Origin` / `Referer` like a browser.
* **Cookies.** The kit keeps the browser's cookie jar and sends `Cookie` itself, because the production cookies are
  `__Host-` / `Secure`.
* **Redirects** are followed hop by hop, and each hop gets its own stats line (`POST /auth/login -> GET /me`). A page
  view that lands on another page counts as a failure.
* **Sign-in** like the browser: the form is parsed (its hidden fields, "Remember this account" left ticked, the
  device key `02-accounts.js` puts in), and the remembered-account reference the server hands over in its cookie
  moves out of the cookie jar. Members whose browser remembers the account (30 %, `rr` / `rd`) use the **account
  chooser**: `POST /auth/remembered` with the reference, the chosen account's photo, then the form with
  `account_ref` instead of the e-mail.
* **Double-submit protection.** A plain form POST carries the hidden one-time `submit_id` of that form on the page it
  came from (a fresh one per render, so per submission; the cart line's own form for a removal). JSON and FormData
  calls send `X-Submit-Id` with a new id per call, like `IP.api`.
* **One session per account**: a 401 with `X-Signed-Out: elsewhere`, a page view sent to the sign-in page that then
  says "signed in on another device" (the kit follows it once to tell), or a socket closed with **4409** is reported
  as `signed in elsewhere - two simulated users share one account`, a failure.
* **Junk escaping**: every HTML page a normal user loads is checked for the seeded junk's markup arriving
  unescaped (`<script>alert(1)</script>`, `<img src=x onerror=alert(1)>`, `<svg onload=alert(1)>`); a hit is a failure.
* **CSRF** comes from `<meta name="csrf-token">`: `X-CSRF-Token` for APIs, hidden `csrf_token` + `page_id` for forms,
  and multipart FormData for the shop, exactly like the frontend JS.
* **Static assets** (CSS, JS, favicon and up to 6 images) come from the browser cache. The server sends no
  Cache-Control, so the kit keeps them fresh for 10 % of their age, then revalidates with `If-None-Match` and expects
  a 304.
* **Signed-in pages** poll `/api/notifications/unread` at most every 30 s and open a **new WebSocket
  `/ws?page=<id>` per page view**. The previous socket closes with code 4001 in the background, like `pagehide`.
  Connect + hello time is reported as `WS /ws (connect+hello)`.

| class | weight | does |
|---|---:|---|
| AnonymousVisitor | 100 (~46 %) | home; /gyms (pages, city / mode / q filters, near me with lat/lon); gym pages weighted by popularity (branches^0.7, so the big chains get views); /gyms/{slug}/branches (pages); branch pages + map tiles; /plans; /shop (pages, categories, q= searches incl. typos, sort, store filter after the `/api/shop/stores?q=` typeahead); products; /shop/gym/{slug}; /jobs (all, pages, role) + near me with 5/10/20 km and view=all; job pages; trainer profiles; legal pages; sign-in page. A visit lasts ~6 page views; then 70 % of visitors are replaced by a new one (new address, empty cookies and cache) |
| Member | 60 (~28 %) | signs in once (30 % through the account chooser); /me; health (+ history, alerts, logs a reading); notifications page + island feed + read-all; chat list, thread (+ messages catch-up, mark read), posts now and then; shop, products, cart add → cart → remove, wishlist save + remove, **checkout** (form parsed like a browser → place order → test-mode payment approve → order page); orders, calendar, tasks, account, subscriptions, coach, videos, wellness, own gym; **AI** (≤ 1 ask per 90-135 s): assistant (loads its history first, like the page), streamed plan question, streamed product question. Each answer is classified: answer, cached, fallback, busy or blocked; busy or blocked counts as a failure |
| Owner | 20 (~9 %) | dashboard; staff (pages, filters) + detail; customers (pages, search, status) + detail; shop dashboard, orders (+ status, pages, detail), products (+ search); finance + transactions (pages, direction); audit; payroll; gyms; jobs + **enquiries**; team chat; notifications; rarely the CSV export. The 3 biggest chains' owners come first |
| Staff | 10 (~5 %) | portal, schedule, clients + detail, chat + thread + replies, notifications, account. The 4 trainers with 200 clients come first |
| Superadmin | fixed 3 (1.5 % at 200 users) | each with its own account (`lt_superadmin_01..10`): overview, security centre, trust & safety, users (pages, search), audit, bans, AI page, review queue, outbox, the load-test page. It never opens /superadmin/selfcheck, which probes external services. Admins do not scale with traffic |
| JunkUser | 22 (~10 %) | random junk into every door it can reach - section 6a. Stats names start with `JUNK` |
| Abuser | 5 (~2.3 %) | each pattern has a fixed address, and half of the bursts come from a fresh botnet address: login brute force on an unknown account; a flood of one repeated assistant message (its own lt_abuser account); 80-150 identical `/shop?q=` requests; a 404 directory scan; scanner signatures (SQLi, XSS, traversal, JNDI, sqlmap or Nikto User-Agent); a WebSocket flood (50 pings in 10 s, which the server must close with 4429). Expected: 400, 401, 403, 404, 429. A 5xx, a successful wrong-password sign-in or an unstopped flood is a failure - except a 503, a timeout or a socket turned away with 1013 in a step where the normal users were shed too (overload; section 7). Its own sign-in (`(own account)`) and the sign-in page before it are setup, never judged. Stats names start with `ABUSE` |

**Status checks** use `catch_response`. Every final response must have the expected status: pages 200 after
redirects, chat posts 201 (409 for a read-only thread), shop add and wishlist 200 or a 400 shop refusal, checkout
200 or 400. These all count as **failures**:

* a redirect to `/auth/login` while signed in;
* an error page served with status 200;
* any 5xx;
* a **429** for a normal user;
* a transport error;
* an AI answer that is busy or blocked;
* "signed in elsewhere" on the user's own session (two simulated users share an account);
* the seeded junk's markup arriving unescaped in a page.

**The app's caps** (`app/core/caps.py`) stay out of reach of the normal users in 10 minutes: a member places at most
one order per checkout task (about one in 10 minutes; the seed leaves at most a few unpaid orders per member, cap 10,
and 30 orders a day), adds one bag line and removes it again (bag 50 lines / 30 per store), saves and removes the same
wishlist item (500), posts into existing threads only (20 new threads, 500 messages a day) and creates no tasks itself
(300 a day; the heavy members' seeded tasks are spread over months). No normal user checks out a gym membership (3
online + 1 in-person), and job enquiries come only from the JunkUsers - members are job seekers; owners, employed
staff and super-admins would get 403 `job_seekers_only`, which is their documented answer. A cap the junk senders reach
(409 / 429) is an expected 4xx.

## 6a. Random junk data

The owner asked for "a 10-minute Locust stress test with random junk data". There are two halves.

**Junk in the database** (`seed_loadtest.py --junk-share`, default 0.10, `--no-junk` for none). 10 % of the seeded
values of these columns are replaced by random junk, from a random generator of their own (`JUNK_SEED`), so every
rebuild is the same and the rest of the data does not move:

| table | columns |
|---|---|
| users, employees | full_name (+ employees.bio) |
| gyms, branches, branch_photos | name, tagline, description; branch name, address line; photo caption |
| products, product_reviews | name, brand, description; review title and body |
| customer_profiles, health_conditions | health notes; condition notes |
| chat_threads, chat_messages | subject, last-message preview; message body |
| tasks | title, notes |
| job_postings, job_enquiries | title, description, requirements, location; applicant name, message |

A full seed writes about 63,000 junk values (`seed_summary.json` "junk": per column, with samples). The generator
(`ltlib/junk.py`, standard library only) makes: emoji and other 4-byte characters (ZWJ sequences, flags, skin tones,
CJK extension B, mathematical letters); right-to-left and mixed-direction text with bidi controls (RLO, LRI ...);
zero-width characters inside words and "Zalgo" stacks of combining marks; strings of exactly the column's maximum
length (VARCHAR(n) from the app's metadata; TEXT columns 1,000-4,000, chat bodies within their CHECK of 1..4000);
whitespace-heavy strings (tabs, newlines, NBSP, em / ideographic spaces, line separators); HTML / JS / SQL / template
strings (`<script>alert(1)</script>`, `' OR 1=1 --`, `{{7*7}}`, `${7*7}`, `</textarea>` ...); control characters.
Every value keeps a visible character, and nothing that a normal user puts into a URL is touched (e-mails, slugs,
cities, categories), so normal requests never carry junk. Every HTML page a normal user loads is checked for the
junk's markup arriving unescaped (section 6).

**Junk in the requests**: the `JunkUser` class (weight 22, ~10 % of the users; `LT_JUNK_WEIGHT`).

* **Who.** Half anonymous - from their own block 198.19.128.0/19, half of them "browser-like" (Sec-Fetch headers, so
  the gate never bans them for a GET) and half tool-like - and half signed in with an account of their own
  (`lt_junk_001..500`, online members of one gym, fixed addresses of their own). A sender the gate bans (403
  "Access blocked") comes back from a fresh address of its block - a signed-in one with the same session from
  198.19.160.0/19 - so the junk keeps reaching the app for the whole run. No junk address is ever a normal user's.
* **What.** Public pages and APIs with random and missing query parameters (negative / huge / float / NaN / Infinity
  numbers, oversized and unicode / control-character strings, NUL bytes, duplicated keys, broken UTF-8 escapes);
  random paths and junk where ids and slugs go; sign-in, sign-up, password and username recovery, one-time links and
  the account chooser; **every form** of its pages, parsed like a browser (csrf_token and the form's own submit_id
  kept, sometimes dropped) with fields junked, emptied, dropped, doubled or sent with the wrong encoding (JSON,
  text/plain, multipart), plus a resubmission of the same submit id now and then (the double-submit guard must replay
  or answer 409); junk bytes uploaded as images (random bytes, truncated JPEG / PNG / GIF / WebP headers, a PNG
  "decompression bomb" header, SVG with script, odd file names); the JSON APIs with malformed JSON, wrong types,
  5,000-level nesting, 5,000-digit numbers, NUL bytes and wrong content types; other users' orders, threads, tasks,
  notifications, alerts, documents, staff, payroll ... by id (**IDOR**: `catalogue.json` "foreign"); odd methods
  (PUT, DELETE, TRACE, PROPFIND ...) and headers (Host, Cookie, Accept, Range, X-Submit-Id ...); junk frames on its
  WebSocket.
* **Never**: an identifier that could be a real account (a junk guess must never lock, e-mail or sign in a simulated
  user - one session per account), its own sign-out, account deletion or "sign out other devices", the
  X-Forwarded-For / -Proto the proxy sets, or a decodable image (no OCR / AI job starts from junk). Well-formed AI
  asks (assistant, plans, product questions, coach, live coach, TTS) at most one per `LT_JUNK_AI_INTERVAL` (150 s)
  per sender, because the AI slots are shared with the members.
* **Expected:** any 2xx / 3xx / 4xx - validation errors, 401 / 403 / 404, the caps' 409 / 429, 413, 422, the gate's
  429 / 403 and bans. **FAILURE:** any 5xx, a timeout or connection error, a 2xx on another user's object (IDOR), a
  redirect to another site, junk markup reflected unescaped, a WebSocket closed with 1011 or dropped, "signed in
  elsewhere". Stats names start with `JUNK` (`JUNK POST /api/chat/[id]/messages`, `JUNK GET /shop/orders/[id] (IDOR)`).
  Every failure is written with its input to `results/<run>/junk_failures_<pid>.jsonl`.

## 7. Results: monitor, report, summary.json

`monitor.py` writes `results/<run>/monitor.csv` every 5 s:

* CPU %, RSS and **USS** of the uvicorn master and every worker;
* the Locust processes' CPU, as a sum and the **busiest one**;
* CPU of the PostgreSQL backends of `ironpulse_loadtest`;
* connections of `ironpulse_loadtest` by state (active / idle / idle in transaction / waiting on locks, longest
  query), plus the whole cluster's and the other databases' connections;
* machine CPU %, load average, and the CPU of everything that is not this test, with its top 5 processes;
* the TCP `listen queue overflow` counter and TIME_WAIT sockets;
* Redis instance memory (and its change since start), db 9 clients and keys;
* cumulative `server.log` counts: 5xx, 429, Tracebacks, timeouts, QueryCanceled, pool timeouts, ERROR, worker deaths,
  "Exception in ASGI application" and DB-timeout warnings; `log_5xx_junk` = the 5xx answered to a JunkUser address
  (the access log shows the X-Forwarded-For client; `ltenv.junk_ip`).

`pg_slow.csv` holds queries active for more than 1 s, with their text and wait event.

`analyze.py <run> [--slo-users 200]` reads `requests_*.csv` (one row per request, so step percentiles are exact),
Locust's CSVs, `locust.log`, `monitor.csv`, `pg_slow.csv`, `server.log`, `run_meta.json` and `server_config.json`.
It writes **`report.md`**:

* the verdict;
* per load step: RPS, errors, p50/p95/p99 (all and pages only), CPU ms per request (server and PostgreSQL), PG
  connections;
* overload evidence per step: 5xx/503, DB timeouts, AI busy, WS connects/s and connect p95, static KB/s and 304s,
  new visitors/s, abuse RPS and status mix, Redis fallbacks of the shared limits, background jobs, load-generator
  CPU, other CPU, load average, cluster connections;
* the knee, a capacity estimate and the recovery;
* per class, AI outcomes, checkout outcomes, per endpoint, abusers (first refusal, the failures counted, the overload
  failures not counted and each pattern's verdict: `refused`, `setup`, **not throttled** or **failed**), resources,
  server.log and the top failures;
* **Random junk input**: the junk senders by kind, their status mix and 4xx breakdown, the 5xx counted by the junk SLO,
  server-side 5xx to junk addresses, the IDOR probes (and where the refused ones were redirected), a table per junk
  endpoint (5xx first), and each failing junk request with its URL, input, body and the server's traceback
  (`server.log` "unhandled error on ..." within 10 s on the same path);
* the run setup: pinned and effective settings, stand-ins, platform and PostgreSQL settings.

It also writes **`summary.json`** (schema version 1, docs/load-test-page.md) for `/superadmin/load-test`. That file
is validated **before** it is written by the app's own `app.services.loadtest_results.parse_summary`, run with
`.venv/bin/python` (or `LT_PROJECT_PYTHON`). A rejection is a hard error, exit status 2, naming the field path (for
example `endpoints.3: failures (410) cannot exceed requests (405)`), and nothing is written. Locust's own
`locust_report.html` sits next to it; its "Aggregated" row includes the abuse traffic.

### Production SLOs and the verdict

The SLOs are judged **only inside the SLO step** (the step with exactly `--slo-users` users, default 200), in its
steady part:

* error rate **< 1 %**, abusers and junk senders excluded (and AI excluded when the run had no AI provider);
* page **p95 < 1 s** and page **p99 < 2.5 s**;
* **zero 5xx**, seen by the clients and in server.log (the junk senders' are judged by their own row below);
* **zero DB statement or pool timeouts**;
* AI asks of members never busy or blocked.

The **whole run** fails on:

* an unhandled HTTP 500, a Traceback or "Exception in ASGI application";
* a worker restart (uvicorn "Child process died", an extra "Started server process", or a worker PID change);
* a task of the kit that crashed;
* **`abusers throttled`** - abusers that were never throttled: every attack pattern must be refused as designed (at
  least one 429 / 403 - 404 for the directory scan, 400 counts for the scanner signatures - and the WebSocket flood
  closed with 4429 or refused at the handshake), and no abuse request may fail (5xx, timeout, a successful
  wrong-password sign-in, an unstopped flood ...) - **except** a 503, a transport / connect timeout or a socket turned
  away with 1013 "try again later" in a step where the normal users were shed too (a 503, a transport error or a
  socket turned away for overload of a normal user): the server's overload, not the abuse - the junk verdict's rule.
  Those are **reported separately** (the abusers table's `overload (not counted)` column, a "Not counted" list with
  their steps, the verdict detail, and `summary.json` `abuse.overload_not_counted`). A pattern with no 429 / 403 at
  all, or a flood never closed with 4429, fails in any step. The abuser's own sign-in (`ABUSE POST /auth/login (own
  account)` and the page it is sent to) and the sign-in page it loads first are setup (verdict `setup`), never judged
  as abuse;
* "signed in elsewhere" anywhere (`kit: one account per simulated user`);
* **`zero 5xx under random junk input`** - the junk SLO row (`report.md` and `summary.json` `verdict.slos`): every
  5xx, timeout / connection error and WebSocket error (1011 / dropped) of a junk request fails it, except a 503 or
  timeout in a step where the normal users were shed too (overload, not the junk; reported separately);
* `junk: other users' objects refused, no open redirect, junk rendered escaped` - any IDOR, open redirect or
  unescaped junk.

`summary.json` also carries a `junk` object (requests, status mix, 4xx breakdown, 5xx per endpoint; the page's
schema-1 loader ignores it) and a high-severity finding per junk 5xx endpoint and per IDOR / open redirect / XSS hit.

The 503s with Retry-After that the app sends when a pool or statement timeout hits are its designed load shedding.
In the overload steps they are **reported per step as evidence of the knee**, not counted as failures. The WebSocket
handshake sheds the same way: when its session check cannot get a database connection the server accepts and closes
the socket with **1013** ("try again later"; the page reconnects with a jittered back-off) - the kit reports it as
`closed before hello: 1013 try again later ...`, a failed `WS /ws (connect+hello)` of that step. Overload in the
background work (presence, jobs, worker loops) is one `database pool timeout in ...` WARNING a minute per call site,
never a traceback, and counts as a pool timeout of its step like the 503s' lines.

**INCONCLUSIVE**, never PASS, when:

* no step has exactly `--slo-users` users (the SLO rows show "n/a");
* the SLO step is **CONTAMINATED**: other programs used more than one core *while the whole machine was more than 85 %
  busy* (a steady background such as an IDE on a machine with spare cores is reported in the step table but does not
  void the step), the load average exceeded the CPU count, or the cluster had more than 90 connections;
* the run was started with the preflight overridden (`--force` / `LT_FORCE=1`) *and* the SLO step is missing or
  contaminated - a forced start whose SLO step was clean is reported as a "machine isolation" PASS row instead;
* the load generator was saturated;
* the run started on a busy machine with `--force`.

**The knee** is the first rising step whose page p95 exceeds 1 s or whose error rate exceeds 1 %. The capacity is
the last rising step before it. report.md counts its requests/s for normal users; summary.json (`steps[].rps`,
`capacity.max_rps_within_slo`, the pages' charts and tiles) counts every request the server answered, abusers and
junk senders included, so the tile and the step table always agree. **Recovery** is the time until page p95 is under 1 s and errors are under 1 % again,
held for 20 s, plus the time until PostgreSQL's active connections are back to the SLO-step level.

**Memory** is judged by worker USS, the memory unique to each worker. macOS RSS moves with memory compression and can
even shrink. A leak verdict needs at least 10 minutes at constant load (`LT_PROFILE=soak`: worst worker's USS slope
under 3 MB/min). In the 10-minute step test, memory is shown as information only.

## 8. Baselines (functional smokes, not the verdict)

### Junk smoke on 2026-11-13 (the kit after the app's changes of 2026-11-13)

Run `smoke-20261113-104921`: `LT_PROFILE=smoke`, 2 minutes, 14 users (2 visitors, 3 members, 1 owner, 1 staff,
2 super-admins, 1 abuser, 4 junk senders: 2 anonymous, 2 signed in), `--processes 2`, AI simulated, on a fresh seed
(120 s, 63,016 junk values, 2,723 products' rating summary backfilled). The machine was busy (load average up to 6.4,
other programs up to 4.2 cores), so the run is INCONCLUSIVE by design; every check passed:

* **Traffic:** 5,365 requests, **0 failures** in every class, no 5xx, no Tracebacks, no DB timeouts, no worker
  restarts, no "signed in elsewhere" (2 super-admins with their own accounts), 0 task crashes.
* **Junk:** 690 junk requests - 297 2xx, 128 3xx, 257 4xx (400: 77, 401: 31, 403: 47, 404: 45, 405: 13, 422: 44),
  **0 5xx**, 0 timeouts, 0 WebSocket errors; 59 IDOR probes all refused (403 / 404 / sign-in redirect); no open
  redirect, nothing unescaped; one anonymous and one signed-in sender were banned by the gate and came back from a
  fresh address. `zero 5xx under random junk input`: PASS.
* **Seeded junk:** every page of every class rendered it escaped (no `<script>alert(1)</script>` & co. in any page).
* **New paths:** 2 sign-ins through the account chooser, 7 orders placed and paid with the forms' submit ids,
  `/superadmin/trust` and `/superadmin/load-test` viewed, 14 AI asks answered (none busy / blocked).

### Moderate smoke on 2026-11-12

Run `smoke50-20261112-234256`: 3 min, 10 → 50 → 10 users, `--processes 2`, AI simulated.

The smoke ran while the other teams' test suites ran. The load average rose from 7 to **33** on 10 CPUs and other
programs used up to 4 cores, so every step is marked CONTAMINATED and the run is INCONCLUSIVE.

* **Traffic:** 8,390 requests, **0 failures** in every class, no 5xx, no Tracebacks, no DB timeouts and no worker
  restarts.
* **Latency:** page p95 at 50 users was 681 ms. It was 57 ms in the Nov 10 smoke50, at load ≤ 8. Our workers used at
  most 91 % of 400 % CPU and no query ran longer than 1 s: the latency was queueing for CPU.
* **AI:** 16 simulated AI asks answered, none busy or blocked.
* **Checkout:** 7 orders placed and paid in test mode.
* **WebSockets:** 3-9 connects/s, with no drops.
* **Abuse:** every pattern was refused. Brute force from attempt 11, the AI flood from 7, identical requests from 76,
  the 404 scan and scanner probes at once (bans), and the WebSocket flood was closed.
* **Resources:** worker USS 562 → 808 MB, PostgreSQL connections at most 33 of the budget of 64, and 5 short Redis
  fallbacks of the shared rate limits.

## 9. Known differences from production, and limits

* **Runtime.** The kit runs the app on the project's `.venv` (Python 3.15.2, macOS arm64) with 4 uvicorn workers,
  like `scripts/manage.py run --workers 4`. The Docker image in `docker/` may use another interpreter and worker
  count. The report records the platform.
* **Shared machine.** PostgreSQL is shared with the dev servers and the other teams' tests (shared_buffers 128 MB).
  The kit changes only per-database settings (`jit=off`, `random_page_cost=1.1`). It does not set
  `log_min_duration_statement`, which would write into the shared cluster's log; `pg_slow.csv` samples slow and
  idle-in-transaction connections instead.
* **macOS accept queue.** The backlog is capped at 128 (section 4).
* **Integrations are stand-ins:** mail is spooled or goes to the sink, map tiles are a transparent PNG, payments run
  in TEST MODE, and AI is simulated.
* **Not covered:** owner actions that notify every member of a gym (`_notify_members` fan-out). They change branch
  state and would alter the shared data for the rest of a run, so they need a separate scenario on a dedicated gym.
* **Super-admin accounts.** At most 10 simulated admins (`lt_superadmin_01..10`, one account each).
* **Junk is bounded.** The junk senders never send what a browser cannot (header values are one line of latin-1),
  never a decodable image, and only a few well-formed AI asks; a request the client itself cannot send is skipped
  and counted in the report ("could not send").

## 10. Files

| file | what |
|---|---|
| `ltenv.py` | names, ports, paths, the server environment (pinned settings, AI/SMTP modes, generated secrets), client-IP plan |
| `seed_loadtest.py` | database build, bulk data, history, heavy tail, accounts/catalogue, `--reset-runtime` (project interpreter) |
| `run_server.sh` | start / warmup / stop / status of server + monitor (+ SMTP sink) |
| `ltlib/procs.py` | PID registry: identity = PID + start time + command line |
| `ltlib/preflight.py` | is the shared machine quiet? |
| `monitor.py` | 5-second resource sampler (`monitor.csv`, `pg_slow.csv`) |
| `smtp_sink.py` | optional local SMTP sink (discards every mail) |
| `locustfile.py`, `ltlib/browser.py`, `ltlib/ws.py`, `ltlib/data.py`, `ltlib/reqlog.py` | user classes (JunkUser included) + load shapes; browser emulation (submit ids, signed-in-elsewhere and junk-escaping checks); WebSocket presence + flood; test data + account allocator; per-request log |
| `ltlib/junk.py` | the random junk generator (seeded data and requests), a forms parser, junk files |
| `analyze.py` | `report.md` + validated `summary.json` |
| `requirements.txt`, `setup_venv.sh` | the load generator's venv |
| `results/<run>/` | server.log, server_config.json, server_ready.json, preflight_server.json, monitor.csv, pg_slow.csv, locust_*.csv, requests_*.csv, junk_failures_*.jsonl, locust.log, locust_console.txt, run_meta.json, locust_report.html, report.md, summary.json |

Generated and gitignored: `accounts.json`, `catalogue.json`, `seed_summary.json`, `.secrets.env`, `.procs.json`,
`.current_run`, `.server.pid`, `.monitor.pid`, `.smtp.pid`, `results/` and `var/`. Nothing here has been committed.
