# Architecture

Local single-user job tracker for OnlineJobs.ph. FastAPI + SQLite backend,
vanilla-JS frontend, SSE for live streams. One user, one machine, one
database file — there is no auth layer and no multi-tenant design
(spec decision D1).

## Process layout

```
python main.py
  ├─ init_db()                 # schema + versioned migrations (user_version)
  ├─ initial_skills_refresh()  # refresh skill taxonomy (first run; self-heals if < 100 entries)
  └─ uvicorn (app.server:app)
       ├─ event loop (main thread) — all HTTP + SSE
       ├─ lifespan shutdown     — deps.close_all(): closes every app DB conn
       ├─ SIGINT/SIGTERM        — stop in-flight run, drain ≤10 s, summary line
       └─ daemon thread: auto-run scheduler (app/scheduler.py)
```

## Components

| Module | Responsibility |
|---|---|
| `main.py` | Entry point: DB init, skills refresh, uvicorn boot, graceful Ctrl-C (W2.9) |
| `app/server.py` | FastAPI app — REST + SSE routes (see endpoint list below) |
| `app/schemas.py` | Pydantic request models |
| `app/sse.py` | SSE framing + `hardened()`: catches generator exceptions so a run error ends the stream with `done "failed"` instead of killing the client |
| `app/config.py` | Live config (W1.4): merged `config.json` + `config.local.json`, `reload()` without restart, secret redaction |
| `app/deps.py` | Per-(thread, db-path) connection registry (W2.1); all connections closed on shutdown |
| `app/scheduler.py` | Auto-run daemon thread; **run registry** (run_id → StopToken), `stop_run`, `wait_runs`, `record_run_status` (W2.5/W2.8/W2.9) |
| `db/connection.py` | Schema DDL, `get_conn()` (30 s busy timeout), `init_db()` (migration-gated), config/DB-path accessors |
| `db/migrate.py` | Versioned migration registry: steps 1..N, applied `current+1..SCHEMA_VERSION`, `PRAGMA user_version` bumped only after each step; `python -m db.migrate --dry-run` (W2.7) |
| `db/repos/jobs.py` | Job CRUD, queries, status history |
| `db/repos/skills.py`, `db/repos/settings.py` | Taxonomy store; app_settings key/value (scheduler state, instance lock W2.6) |
| `scraper/client.py` | Persistent `requests.Session`: 1 s throttle, exponential backoff on 429/5xx honoring `Retry-After`, browser UA, **per-run StopToken** (W2.8) |
| `scraper/parsers.py` | HTML → structured data: search-list boxes, detail pages, closed-listing detection |
| `scraper/pipeline.py` | `harvest()` / `enrich()` — pure event generators (`page`/`job`/`log`/`error`/`summary`) |
| `scraper/skills.py` | Skills taxonomy API client (first-run refresh) |
| `resumes/` | Multi-profile masters (`schema.py`), deterministic ATS scorer (`ats.py`), LLM tailor (`tailor.py`), docx/txt render (`render.py`), source digest (`digest.py`), RenderCV one-page CV (`yamlcv.py`) |
| `static/` | Single-page dashboard UI (vanilla JS) |

## Endpoint list

| Method & path | Behaviour |
|---|---|
| `GET /` | Dashboard HTML |
| `POST /api/pipeline/run` | Scrape with scope `{keyword, categories, skills, posted_since}`; SSE stream of the run |
| `POST /api/pipeline/check` | Re-check stale detail pages; SSE stream |
| `POST /api/pipeline/stop` | `{run_id}` stop that run; no body → active run |
| `GET /api/events` | Long-lived SSE: new-job batches, closures, salary changes, due follow-ups, structure-change alerts, auto-run keyword hides, reset alerts |
| `GET /api/schedule` | Auto-run state: on/off, interval, last run/status |
| `POST /api/schedule` | Set auto-run on/off, interval 1–24 h |
| `GET /api/runs` | Recent runs: kind (harvest/check/auto), scope, inserted/enriched/closed/errors, completed/stopped/failed |
| `GET /api/jobs/{job_pk}` | One job by DB row id |
| `PATCH /api/jobs/{job_pk}/status` | Set status (New / Applied / Interview / Hired / Rejected / Hidden) |
| `PATCH /api/jobs/{job_pk}/notes` | Owner notes (404 if the job is gone) |
| `PATCH /api/jobs/{job_pk}/follow-up` | Follow-up date (404 if the job is gone) |
| `POST /api/jobs/{job_pk}/recheck` | Re-fetch that job's detail page now and apply it (closed on 404/410) |
| `GET /api/stats` | Table counters for the UI header |
| `POST /api/jobs/reset` | Soft-delete every job + status history (rows stay, `deleted_at` set) — Undo restores them |
| `POST /api/jobs/reset/undo` | Bring back everything the reset hid |
| `GET /api/keywords` | Saved keyword rules (auto-hide lists) |
| `POST /api/keywords/apply` | Apply + persist keyword rules (`pay_goal_monthly` rescues by pay); empty lists are a no-op, `clear_rules: true` is the explicit wipe |
| `GET /api/scrape-scope` | The persisted auto-run scope |
| `POST /api/scrape-scope` | Persist the auto-run scope |
| `GET /api/skills` | Skill taxonomy (local table) |
| `GET /api/skills/categories` | Category list |
| `POST /api/skills/refresh` | Re-fetch the taxonomy from the site |
| `GET /api/jobs` | Query: search, status, salary, `min_fit`, `min_ats`, date range, keyword filters, `hide_reposts`, `include_deleted` |
| `GET /api/jobs/export` | Full CSV (no pagination, same filters as the table, BOM for Excel) |
| `GET /api/resume` | Master resume (default profile) |
| `POST /api/resume/fit` | Score listings the browser extension is looking at (X-E bridge, max 50 per request; `fit_max` says how much of the 60-point scale the listing actually made scorable) |
| `PUT /api/resume` | Save a named profile |
| `GET /api/resume/profiles` | Named profiles + which is default |
| `GET /api/resume/ats` | Deterministic ATS score: `fit` (/60) + `hygiene` (/40), `auto=1` = best-fitting profile |
| `POST /api/resume/tailor` | LLM rewrite of the best-fitting profile for a job, guarded for faithfulness and stored |
| `GET /api/resume/export` | Tailored resume as docx/txt — served from the stored copy, so it matches what was scored |
| `POST /api/resume/build` | One-page Harvard CV (RenderCV) as an SSE run: per-round progress, stoppable |
| `GET /api/resume/built` | Built CVs on disk (pdf/docx) |
| `GET /api/resume/built/{name}` | Download one built CV |
| `GET /api/resume/yamlcv-status` | Whether RenderCV + a LaTeX engine are available |
| `GET /api/config`, `POST /api/config/reload` | Live config (redacted) + reload (rebuilds the HTTP client too) |
| `GET /health` | Real health: DB probe + site probe; 200 / 200-degraded / 503-degraded |

## A run, end to end

```
POST /api/pipeline/run
  → deps.get_db() (registry conn, schema ensured)
  → in-process pipeline lock → cross-process instance lock (settings row,
    PID-liveness checked)                                   [W2.6]
  → scheduler.begin_run(run_id, StopToken, shared OJClient)
  → pipeline.harvest() → events streamed over hardened() SSE
  → pipeline.enrich()  → parallel detail fetches (config workers)
  → record_run_status(): completed / stopped / failed → app_settings
  → finally: end_run() + release lock
```

The auto-run scheduler executes the same `run_once` in its daemon thread;
the shared pipeline lock guarantees **at most one run at a time** across
manual runs, checks, and auto-runs (W2.6). Each run mints its own
`run_id` + `StopToken`; the stop button (and Ctrl-C) flip exactly that
token, checked between pages and jobs (W2.8). Stopped runs record
`stopped` with partial results kept; only raised errors record `failed`.

## Data

One SQLite file (`jobs.db`, WAL, 30 s busy timeout): `jobs`,
`job_history` (status transitions), `skill_tags` (site taxonomy with
parent + category path), `app_settings`
(key/value — scheduler state, keyword rules, scrape scope, last-run
status). `resumes/masters.json` holds resume profiles (gitignored);
backups are `VACUUM INTO` snapshots in `backups/` (see operations.md).

### Money

`salary` is the poster's own words and is never rewritten. Everything derived from
it says how it was read: `salary_min/max` are the posted figures, `salary_currency`
the currency the text states (or `salary_assumed_currency=1` when it had to be
guessed from magnitude), `salary_unit` + `salary_hours` + `salary_hours_basis`
(`stated` / `full-time` / `part-time` / `unstated`) how a month would be built, and
`salary_monthly_min/max` the PHP/month figure — NULL whenever the listing does not
support a month (no stated hours, a piece rate, or no live FX rate for that
currency). `salary_rate_min/max` carry the PHP per hour / per day / per item, which
stays honest when a month is not. Migration v12 re-derives all of it from the raw
text; `renormalize()` does the same daily with live rates.

### Keyword rules

`jobs.keyword_hit` (migration v13) records the rule that touched a job: `negative:crypto`,
`no positive keyword`, or `rescued:crypto ₱60,000/mo` when the listing clears the stored pay
goal (`settings.pay_goal_monthly`) and so stays visible. A hide that names no word is a hide
you cannot argue with.

### Listing flags

Migration v14 derives three facts from text the DB already holds: `off_platform`
(`scraper/offplatform.py` — an application ask, a messaging tool *inside* that ask, an
external link, an address, or the `----------` OJ.ph leaves where it stripped a link),
`over_40h` (the listing asks for more than a 40-hour week), and `superseded_by` (this row
is the older copy of a repost — the one holding your status and history).

## Concurrency model

- **Event loop** (main thread): all HTTP/SSE. Long work is delegated.
- **Auto-run thread**: one daemon; polls every 60 s; fires when
  `now >= next_run`.
- **DB connections**: per-(thread, path) registry; `check_same_thread`
  disabled; WAL lets readers proceed during writer transactions.
- **Runs**: one at a time (pipeline lock). Stop tokens are per-run.
- **Shutdown**: SIGINT/SIGTERM → stop in-flight run → uvicorn drains
  (force cap 10 s; lifespan `deps.close_all()` inside) → `wait_runs(10)`
  for the auto-run thread → summary line (W2.9).
