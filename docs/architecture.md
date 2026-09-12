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
| `GET /api/events` | Long-lived SSE: new-job batches, closures, salary changes, due follow-ups, structure-change alerts, auto-run keyword hides |
| `GET/POST /api/schedule` | Auto-run on/off, interval 1–24 h, last run/status |
| `GET /api/jobs/{job_pk}` | One job by DB row id |
| `GET /api/stats` | Table counters for the UI header |
| `POST /api/jobs/reset` | Delete every job + status history (keeps rules/scope/scheduler/resumes) |
| `GET /api/keywords` | Saved keyword rules (auto-hide lists) |
| `POST /api/keywords/apply` | Apply + persist keyword rules |
| `GET/POST /api/scrape-scope` | Persist the auto-run scope |
| `GET /api/skills`, `GET /api/skills/categories`, `POST /api/skills/refresh` | Skill taxonomy (local table; refresh re-fetches from the site) |
| `GET /api/jobs` | Query: search, status, salary, `min_ats`, date range, keyword filters |
| `GET /api/jobs/export` | Full CSV (no pagination) |
| `PUT/GET /api/resume` | Master resume profiles |
| `GET /api/resume/ats` | Deterministic ATS score (0–100), `auto=1` = best-fitting profile |
| `POST /api/resume/tailor` | LLM rewrite of the best-fitting profile for a job |
| `GET /api/resume/export` | Tailored resume as docx/txt |
| `POST /api/resume/build` | One-page Harvard CV (RenderCV) |
| `GET /api/config`, `POST /api/config/reload` | Live config (redacted) + reload |
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
