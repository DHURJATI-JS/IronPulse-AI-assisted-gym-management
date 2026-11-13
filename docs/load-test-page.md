# Load-test results page

`GET /superadmin/load-test` shows the results of the 10-minute production-like Locust stress test to super-admins:
the SLO verdict, the capacity the run found (max users and requests per second within the SLOs, the knee), latency
and throughput per load step, server CPU / memory / database connections over time, the slowest endpoints, user
classes, abuse throttling, server errors, the findings and the exact run settings.

The page only reads files written by the load-test kit (`loadtest/`, see `loadtest/README.md`). It never touches the
database beyond the super-admin session check, and it has no JavaScript.

A curated summary of the same results is public: `GET /load-test`, "Load test results" in everyone's navbar - see
[Public page](#public-page).

| What | Where |
|---|---|
| Routes, page model, chart geometry | `app/routers/superadmin_loadtest.py` |
| Public page (`/load-test`) | `app/routers/public_loadtest.py`, `app/templates/public/load_test.html`, navbar item `app/core/nav.py` |
| `summary.json` schema (pydantic) + loader | `app/services/loadtest_results.py` |
| Templates | `app/templates/superadmin/load_test.html`, `_load_test_*.html` (charts: `_load_test_charts.html`) |
| Tests + fixture runs | `tests/test_superadmin_load_page.py`, `tests/test_public_load_page.py`, `tests/fixtures/loadtest/` |
| Settings | `LOADTEST_RESULTS_DIR` (`Settings.loadtest_results_dir`), `PUBLIC_LOADTEST_PAGE` (`Settings.public_loadtest_page`) |

## Where the results come from

One folder per run inside the results folder:

```
loadtest/results/<run_id>/
    summary.json         the only file the page parses (schema version 1, below)
    report.md            offered as a download (named by files.report_md)
    locust_report.html   Locust's own HTML report, offered as a download only (named by files.locust_html)
    *.csv, monitor.csv, server.log ...   ignored by the page
```

* `LOADTEST_RESULTS_DIR` unset (or empty) = `<project root>/loadtest/results`; a relative value is relative to the
  project root.
* `<run_id>` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` (for example `20261110-1800-prod-like`). Other folders,
  folders without a `summary.json` (a run still in progress) and files are ignored. A kit file such as
  `report_summary.json` is not read: the page reads `summary.json` only.
* Write `summary.json` **atomically** (write `summary.json.tmp`, then rename it) so the page never reads half a file.
* Write the keys `schema`, `run_id` and `started_at` **first**: the run list orders runs it has not parsed yet by a
  `started_at` read from the first 4 KB of the file (else by the folder's modification time).
* The two downloads can have any bare file name: point `files.report_md` / `files.locust_html` at them (the kit's
  Locust report is `locust_report.html`). The download URLs stay `/superadmin/load-test/<run_id>/report.md` and
  `/superadmin/load-test/<run_id>/locust.html`.

## `summary.json` - schema version 1

One UTF-8 JSON object of at most **5 MB**. Rules that apply everywhere:

* Unknown keys are ignored at every level - add whatever else is useful.
* Every number must be finite: `NaN`, `Infinity`, `-Infinity` and overflowing literals such as `1e999` make the run
  unreadable. Write `null` for a value that was not measured. In Python: `json.dumps(summary, allow_nan=False)` - it
  raises instead of writing `NaN` (`statistics` / percentile helpers return `nan` for an empty series: map it to
  `None` first).
* Text is cleaned (control characters removed, whitespace collapsed) and cut to the length shown (`text≤N`); it is
  never rejected for being long. Lists longer than their cap **are** rejected - downsample before writing.
* Times are ISO 8601 between **2000-01-01 and 2100-01-01** (UTC); without an offset they are read as UTC. A sentinel
  such as `9999-12-31` or `0001-01-01` makes the run unreadable (an error card, never a broken page).
* Units are in the names: `_ms` milliseconds, `_s` seconds since the run started, `_mb` MiB, `_pct` percent 0-100,
  `rps` requests per second. `error_rate` and every `_share` are a **fraction 0-1** (`0.0042` = 0.42 %).
* `?` = optional: the key may be left out or `null`.

| Field | Type | Meaning |
|---|---|---|
| `schema` | `1` | Required. Any other value shows "unsupported schema version". |
| `run_id` | text≤64 | Informational; the page identifies a run by its folder name. |
| `started_at`, `ended_at` | ISO time | Between 2000-01-01 and 2100-01-01; `ended_at` ≥ `started_at`. |
| `duration_s` | number ≥ 0 | Length of the run. |
| `target` | object | {`host` text≤200, `workers` int 1-1024, `environment` text≤40, `redis` bool, `notes`? text≤2000, `db_rows`? {table: row count} (≤ 100 tables) - the dataset size}. |
| `machine`? | object | The load generator: {`cpus` int ≥ 1, `memory_gb`, `os` text≤120, `python` text≤40, `locust_version` text≤40}. |
| `verdict` | object | {`passed` bool, `headline` text≤300, `slos`? list (≤ 50)}. Each SLO: {`name` text≤120, `target` text≤60, `actual` text≤60, `passed` bool, `users`? int}; `users` = the load step it is judged at (else "N users" in its `name`). |
| `capacity` | object | {`max_users_within_slo` int, `max_rps_within_slo` (requests/s of that step, counted like `steps[].rps`: every request the server answered - normal users, abusers and junk senders), `knee_users`? int (the step where latency starts to climb), `note`? text≤1000}. |
| `totals` | object | {`requests` int, `failures` int (≤ requests), `error_rate` 0-1, `rps`, `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`} - every request of the run. |
| `steps` | list ≤ 100 | {`users` int, `start_s`, `end_s` (≥ start_s), `rps`, `error_rate` 0-1, `p50_ms`?, `p95_ms`?, `p99_ms`?, `slo_p95_ms`?, `passed` bool} - shown in `start_s` order. See *Which requests* below. |
| `timeline` | list ≤ 2,000 | One point every 5-10 s: {`t_s`, `users` int, `rps`, `p50_ms`?, `p95_ms`?, `failures_per_s`? (0), `cpu_pct`?, `rss_mb`?, `db_connections`? int} - shown in `t_s` order. `cpu_pct` is the share of the whole machine (per-process percentages summed, divided by the CPU count). |
| `endpoints` | list ≤ 500 | {`name` text≤200, `method` text≤10, `group`? text≤60, `requests` int, `failures` int (≤ requests), `p50_ms`?, `p95_ms`?, `p99_ms`?, `max_ms`?, `rps`}. |
| `classes`? | list ≤ 50 | Locust user classes: {`name` text≤80, `weight_share` 0-1, `requests` int, `failures` int (≤ requests), `p95_ms`?}. |
| `abuse`? | object | The abusive user class: {`requests` int, `throttled` int (≤ requests), `throttled_share` 0-1 (higher is better), `note`? text≤1000}. |
| `server`? | object | {`peak_cpu_pct`?, `peak_rss_mb`?, `rss_growth_mb_per_min`? (may be negative), `peak_db_connections`? int, `pool_size`? int (connections the app's pools keep, all workers), `errors_5xx`? int (0), `timeouts`? int (0), `log_errors`? list (≤ 50) of text≤300}. |
| `findings`? | list ≤ 100 | {`severity` `"high"`/`"medium"`/`"low"`, `title` text≤200, `detail`? text≤2000, `status` `"fixed"`/`"open"`/`"accepted"`}. |
| `files`? | object | {`report_md`?, `locust_html`?} - **bare file names** inside the run folder (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`: no folders, no `..`). Only these two can be downloaded. |

### Which requests the latencies cover

`totals`, `endpoints`, `timeline` and the steps' `rps`, `error_rate`, `p50_ms`, `p95_ms` and `p99_ms` cover **every
request** of their window, as Locust counted it.

`steps[].slo_p95_ms` is the p95 of **only the requests the p95 latency SLO judges**, when that is a subset - this
kit judges page requests only, abusers excluded ("page p95 < 1 s at 200 users"), so write the page p95 there. When any
step has it, the page's p95 step chart plots it against the SLO line and the steps table adds an "SLO p95" column, so
the chart, the table, the KPI tile and the verdict always agree. Leave it out when the SLO judges every request.

The KPI tile "p95 at N users" uses the first SLO whose `name` contains `p95` (N = its `users`, else a number followed
by "users" in its name): it shows that SLO's `actual` - exactly what was judged - with its `target` and met / missed.
Without such an SLO it shows the 200-user step's `slo_p95_ms` (else `p95_ms`). The SLO line on the p95 chart is drawn
at `target` when it holds a number with `ms` or `s` ("< 1 s" = 1,000 ms) and is labelled "judged at N users".

### Complete example

`tests/test_superadmin_load_page.py` parses this example with the real models and checks that it uses every field of
every object above, so it always matches the code.

```json
{
  "schema": 1,
  "run_id": "20261110-1800-prod-like",
  "started_at": "2026-11-10T18:00:00Z",
  "ended_at": "2026-11-10T18:10:00Z",
  "duration_s": 600,
  "target": {
    "host": "http://127.0.0.1:12000",
    "workers": 4,
    "environment": "production",
    "redis": true,
    "notes": "Production settings, seeded with loadtest/seed_loadtest.py.",
    "db_rows": {"users": 24318, "gyms": 120, "transactions": 186420}
  },
  "machine": {"cpus": 10, "memory_gb": 32, "os": "macOS 27.2 (arm64)", "python": "3.15.2", "locust_version": "2.43.1"},
  "verdict": {
    "passed": true,
    "headline": "Holds 200 concurrent users within every SLO; page latency climbs past 350 users (the knee).",
    "slos": [
      {"name": "error rate < 1 % at 200 users (abusers excluded)", "target": "< 1 %", "actual": "0.06 %", "passed": true, "users": 200},
      {"name": "page p95 < 1 s at 200 users", "target": "< 1 s", "actual": "412 ms", "passed": true, "users": 200},
      {"name": "page p99 < 2.5 s at 200 users", "target": "< 2.5 s", "actual": "1,090 ms", "passed": true, "users": 200},
      {"name": "memory growth < 15 MB/min", "target": "< 15 MB/min", "actual": "1.8 MB/min", "passed": true}
    ]
  },
  "capacity": {"max_users_within_slo": 200, "max_rps_within_slo": 405.2, "knee_users": 350,
               "note": "Between 200 and 350 users page p95 almost triples while throughput gains 31 %."},
  "totals": {"requests": 199870, "failures": 582, "error_rate": 0.00291, "rps": 333.1,
             "p50_ms": 74.8, "p95_ms": 640.0, "p99_ms": 1904.0, "max_ms": 6188.0},
  "steps": [
    {"users": 50, "start_s": 0, "end_s": 120, "rps": 104.1, "error_rate": 0.0, "p50_ms": 41, "p95_ms": 104, "p99_ms": 196, "slo_p95_ms": 131, "passed": true},
    {"users": 200, "start_s": 120, "end_s": 360, "rps": 405.2, "error_rate": 0.0006, "p50_ms": 68, "p95_ms": 236, "p99_ms": 470, "slo_p95_ms": 412, "passed": true},
    {"users": 350, "start_s": 360, "end_s": 540, "rps": 531.0, "error_rate": 0.0185, "p50_ms": 410, "p95_ms": 1450, "p99_ms": 2380, "slo_p95_ms": 1210, "passed": false},
    {"users": 700, "start_s": 540, "end_s": 600, "rps": 548.3, "error_rate": 0.0412, "p50_ms": 1320, "p95_ms": 4100, "p99_ms": 7900, "slo_p95_ms": 4870, "passed": false}
  ],
  "timeline": [
    {"t_s": 5, "users": 50, "rps": 101.0, "p50_ms": 40, "p95_ms": 102, "failures_per_s": 0.0, "cpu_pct": 18.5, "rss_mb": 488.0, "db_connections": 6},
    {"t_s": 200, "users": 200, "rps": 404.4, "p50_ms": 69, "p95_ms": 240, "failures_per_s": 0.2, "cpu_pct": 62.0, "rss_mb": 501.2, "db_connections": 24},
    {"t_s": 400, "users": 350, "rps": 529.9, "p50_ms": 402, "p95_ms": 1432, "failures_per_s": 9.8, "cpu_pct": 91.4, "rss_mb": 530.6, "db_connections": 40},
    {"t_s": 600, "users": 700, "rps": 550.3, "p50_ms": 1300, "p95_ms": null, "failures_per_s": null, "cpu_pct": null, "rss_mb": 532.0, "db_connections": 40}
  ],
  "endpoints": [
    {"name": "/owner/finance/transactions", "method": "GET", "group": "owner", "requests": 2410, "failures": 31,
     "p50_ms": 210, "p95_ms": 1840, "p99_ms": 3900, "max_ms": 6188, "rps": 4.02},
    {"name": "/auth/login", "method": "POST", "group": "auth", "requests": 9620, "failures": 120,
     "p50_ms": 230, "p95_ms": 1210, "p99_ms": 2700, "max_ms": 5010, "rps": 16.03},
    {"name": "/gyms", "method": "GET", "group": "public", "requests": 26800, "failures": 0,
     "p50_ms": 44, "p95_ms": 310, "p99_ms": 720, "max_ms": 2400, "rps": 44.67}
  ],
  "classes": [
    {"name": "Member", "weight_share": 0.6, "requests": 120000, "failures": 300, "p95_ms": 690},
    {"name": "Abuser", "weight_share": 0.1, "requests": 8420, "failures": 109, "p95_ms": 31}
  ],
  "abuse": {"requests": 8420, "throttled": 8311, "throttled_share": 0.987,
            "note": "Login floods were answered with 429 by the rate limiter."},
  "server": {"peak_cpu_pct": 91.4, "peak_rss_mb": 532.0, "rss_growth_mb_per_min": 1.8, "peak_db_connections": 40,
             "pool_size": 40, "errors_5xx": 37, "timeouts": 4,
             "log_errors": ["sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 4 reached (31 times)"]},
  "findings": [
    {"severity": "medium", "title": "Database pool saturates at 350 users",
     "detail": "All 40 pooled connections are busy from the 350-user step.", "status": "open"}
  ],
  "files": {"report_md": "report.md", "locust_html": "locust_report.html"}
}
```

## What the page shows

* **Header**: run picker (the 20 newest runs: start time with its time zone, peak users, PASS / FAIL / Unreadable
  chip - a `<details>` menu, no JavaScript) and the two downloads with their sizes.
* **Data volume** (first, in bold - the owner's rule; the same banner opens the public page): the rows of the test
  database during the run (`target.db_rows`) - the total, the number of tables and the main tables in a fixed order
  (users, gyms, branches, memberships, shop products, shop orders, transactions, chat messages, height & weight
  readings; `DATA_VOLUME_TABLES`); nothing when the run recorded no counts.
* **Verdict**: PASS / FAIL, the headline, the capacity note and the SLO checklist (target, actual, pass/fail icon).
* **KPI tiles**: max users within SLO (with the knee), max throughput within SLO, p95 at the SLO step (what the SLO
  judged), overall error rate, total requests, 5xx responses (with timeouts).
* **Load steps**: two small multiples over the same users axis - p95 per step (`slo_p95_ms` when present) with the
  dashed SLO line, and throughput per step - with the knee step shaded, and the steps table (window, req/s, errors,
  p50/p95/p99, SLO p95, result). When a step goes past **3x the SLO** (the spike step), the p95 axis stops near 3x the
  SLO so the other steps stay readable: that column is cut with a double gap (an axis break) and its value is printed
  on its cap; the table and the column's tooltip keep the exact value.
* **Over time**: simulated users, then throughput, failures/s, latency (p50 + p95, labelled at their right end when
  the lines reach it and the labels do not collide), server CPU, memory (RSS, axis starting near its lowest value so
  growth shows as a slope) and database connections against the pool size (with headroom above the pool line); a
  timeline table (every n-th sample beyond 300 rows) in a `<details>`.
* **Endpoints**: the top 25 by p95; `?sort=p95|p99|requests|failures|name` and `?show=all` (at most 500 rows). Failure
  rates from 1 % are a red badge with an alert icon (the row is tinted), anything above 0 an amber badge.
* **User classes**, **abusive traffic** (throttled share as a meter - higher is better), **server health** (peaks,
  memory growth per minute, 5xx, timeouts, log errors), **findings** (severity + status chips, most severe first) and
  the **run settings** (run folder, times, load profile, totals, target, dataset rows, load-generator machine).
* Data the analysis did not record is a neutral grey tile (green is kept for good news such as "no findings").
* Times are shown in the viewer's time zone **with its name** (`23:30–23:40 IST`); a run past midnight shows its end
  date. `summary.json` and the server log are UTC.
* **Empty state** when no run exists (what the page is for, how to run the kit) and an **error card** for a run whose
  `summary.json` cannot be used (the reason, plus a link to the newest readable run). A run that passes the schema but
  still cannot be displayed (a bug) also gets an error card, and the server log has the traceback: one run never
  takes the page down.

An unknown or invalid `?run=` is a 404. Phones: everything fits 375 px - long paths, ids and unbroken text wrap; wide
tables scroll inside their own box.

## Loader rules (`app/services/loadtest_results.py`)

* The run list scans at most **500** entries of the results folder, orders runs newest first by `started_at`
  (fallback: folder modification time) and parses only the **20** it lists (plus the selected run), oldest first, so
  the newest - shown by default - is still cached when the page loads it.
* A run folder, its `summary.json` and every download must resolve (symlinks followed) **inside** the results folder
  (downloads: inside the run folder); anything that escapes is treated as missing. A symlink alias that stays inside
  is allowed and listed once.
* Files are opened as they were checked: no final symlink (`O_NOFOLLOW`), never blocking on a FIFO (`O_NONBLOCK`),
  and the open file must be the regular file (device, inode) that was checked - one swapped in between is refused.
* `summary.json` is read with a **5 MB** cap. Unreadable, oversized, non-JSON, non-finite, out-of-range or
  schema-invalid files become that run's error state (at most 600 characters) - never a 500.
* Parsed runs (and their errors) are cached by (path, mtime, size, inode, ctime) in an LRU of **8**: a rewritten or
  replaced file is parsed again on the next view, even with the same size and a preserved modification time
  (`cp -p`, `rsync -t`, tar). All file work runs in a worker thread.

## Downloads

`GET /superadmin/load-test/<run_id>/report.md` and `/locust.html` serve only the files `summary.files` names, for a
readable run, as regular files of at most **50 MB**, streamed (the database session is released first). Responses
carry `Content-Disposition: attachment; filename="<run_id>-report.md"`, `X-Content-Type-Options: nosniff`,
`Content-Security-Policy: default-src 'none'; sandbox`, `Cache-Control: no-store, private` and the right media type
(`text/markdown` / `text/html`, UTF-8). Locust's HTML report has inline scripts: it is never rendered under the app's
origin. Anything else is a 404.

## Access

Every route uses the same guard as the other super-admin pages (`SuperAdminUser`): anonymous visitors are sent to
the sign-in page, members, owners and staff get a 403. Paths with `../` never reach the page - the security gate
blocks them first - and the loader rejects them anyway. The super-admin page's header links the public page
("Public page") while it is switched on.

## Public page

`GET /load-test` (`app/routers/public_loadtest.py`, template `app/templates/public/load_test.html`) is a curated,
read-only summary for everyone: anonymous visitors and every signed-in role (members, trainers / staff, gym owners,
super-admins). Every navbar ends with **"Load test results"** (`LOAD_TEST_ITEM` in `app/core/nav.py`): in the mobile
menu with its icon, in the desktop bar as a label that never wraps - beside a signed-in role's full menu the bar shows
the item's icon below 1280 px (xl) and the label from there on, always named "Load test results"
(`aria-label` + tooltip); in an unusually crowded bar it is the one item that shrinks, with an ellipsis, instead of
pushing into the buttons on the right. The link is marked active (`aria-current="page"`) on the page. It costs no
query: the menu is built from the settings object.

It shows the **newest run whose `summary.json` is readable** (an unreadable newer run is skipped):

* **first, in bold, the data volume**: the rows of the test database during the run - total, tables and the main
  tables (the super-admin page's banner, `lt.data_volume`); then the disclaimer;
* the verdict (PASS / FAIL, or INCONCLUSIVE when a run that did not pass says "Inconclusive" in its headline - the
  kit's verdict for a busy machine), the headline, the capacity note and the SLO checklist;
* six headline numbers: max users and max requests per second within the SLOs, p95 at the SLO step (what the SLO
  judged), error rate, total requests and **5xx under random junk input** - read from the kit's SLO row "zero 5xx
  under random junk input" (its count of 5xx and whether it held; the number of junk requests comes from the
  `JunkUser` class), or "no junk input in this run";
* the two per-step charts - p95 latency and throughput against users (the super-admin page's SVG macros and
  geometry, with the SLO line and the knee) - and the steps table behind them;
* **how it was tested**: date and time (the viewer's time zone, named), duration, load profile, machine (CPUs,
  memory, OS; "shared with the app server" when the tested host is a loopback address), app workers and settings,
  PostgreSQL / Redis, data volume (total rows and tables, plus users, gyms, branches, memberships and shop products -
  no other table names), the AI mode (`AI simulated` / `AI mode: simulated` / `provider: simulated` in the kit's
  notes, capacity note or SLO names), the Locust version and the simulated user mix (share of each user class with
  a plain-words label, junk senders and abusers included);
* the disclaimer: "Measured on one laptop with simulated AI; not a performance guarantee", pointing to the
  Disclaimer & no liability section of the README and the [Terms](/terms#disclaimer).

A small picker (a `<details>` menu, no JavaScript) offers the **5 newest readable runs** (`PUBLISHED_RUNS`); `?run=`
accepts only those - any other value, an unreadable run included, is a 404, so other run folders cannot be probed.

**Never public** (the super-admin page keeps them): the endpoint table (owner / staff / admin routes), the timeline
(server CPU, memory, database connections), server health and log lines, findings, abuse and IP details, the
per-class table, report / Locust downloads, the target host, notes, run folders, error messages and file paths. The
template never receives the summary itself - only the values `public_view` picks - and every text from the file is
escaped.

No results yet: an empty state. Runs that exist but none can be read or displayed: a neutral "Results unavailable"
card (no error text); a run that fails to display is logged and gets the same card - never a 500. Super-admins see a
"Full report" button to `/superadmin/load-test?run=<run_id>` (nobody else gets the URL). The page reads files only
(the cached, path-safe loader, in a worker thread): no SQL beyond the layout's session lookup.

**Setting** - `PUBLIC_LOADTEST_PAGE` (`Settings.public_loadtest_page`, default `true`): `false` makes `/load-test` a
404 and removes the navbar item for everyone; `/superadmin/load-test` is unaffected (its "Public page" button is
hidden). Restart the server after changing it.

## Charts

Inline SVG drawn by Jinja macros from geometry computed in Python; no JavaScript and no `style` attributes (strict
CSP). Following the data-viz method:

* **Form**: one measure per chart - never two y-axes. Load steps are two small multiples (p95 and throughput) sharing
  the users axis; the timeline is a set of small multiples sharing the time axis.
* **Colour** (validated with the palette checker against the app's real surfaces, white and `#1c1c1e`): one series
  hue - blue `#2a78d6` (dark `#3987e5`); p50 and p95 are ordered, so they are two steps of the same hue (p50 `#86b6ef`,
  dark `#1c5cab`); failures wear the status red `#d03b3b`; reference lines (SLO, pool size) are dashed in muted ink;
  the knee is a 12 % wash of the series hue, and its legend key is the same wash. Colours are Tailwind classes read
  through `currentColor`, with dark-mode values of their own.
* **Marks**: 2 px lines that do not scale with the chart (`vector-effect`), columns with a 4-unit rounded data end and
  a square baseline, area washes at 10 % opacity, hairline gridlines; at most 400 points per series (LTTB
  downsampling). A column cut at the top of a clipped axis carries a double surface-coloured gap.
* **Text**: tick labels are HTML (legible at any width), the unit sits above each y-axis, legends under the charts
  with multiple series or a reference line. Direct end labels ("p95", "p50") are drawn in a uniformly scaled overlay
  (`preserveAspectRatio="xMaxYMin meet"`) with a surface halo, so they never stretch with the plot; they are left out
  rather than nudged away from their lines when they would collide.
* **Hover**: native `<title>` tooltips per step column and per time slice, plus a CSS crosshair on the timeline.
* **Accessibility**: every chart is `role="img"` with `<title>` + `<desc>`; each description points to its table (the
  steps table, or the timeline table - saying which samples it lists when it is strided), and the verdict heading
  reads "Verdict: PASS".

## Tests

`tests/test_superadmin_load_page.py` (run it against a test database of its own: `TEST_DATABASE_URL`) covers access
control, the empty state, the fixture runs, run selection, sorting, traversal and symlink escapes, files swapped
after their check, broken / invalid / non-finite / out-of-range runs, the 20-run limit, downloads, strict-CSP output,
the query count, the cache key, capped errors, clipping, end labels, time zones, the display safety net and this
document's example. The fixture runs are generated by `tests/fixtures/loadtest/make_fixtures.py` (deterministic).

`tests/test_public_load_page.py` covers the public page: the navbar item for anonymous visitors and every role
(desktop + mobile, active state), the fixture run (verdict, numbers, SVG charts, how it was tested, disclaimer), the
kit's junk row and AI mode, that none of the internal details reach the HTML, the latest readable run and the picker,
the empty / unavailable states, `PUBLIC_LOADTEST_PAGE=false`, escaping, strict CSP, the query count and phone widths.
