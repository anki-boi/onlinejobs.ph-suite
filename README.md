# Job Hunter Dashboard

Local job-tracking dashboard for [OnlineJobs.ph](https://www.onlinejobs.ph).
FastAPI + SQLite backend, vanilla-JS frontend, server-sent-events live console.

## Setup

```bash
pip install -r requirements.txt
python main.py                # starts on http://127.0.0.1:8371
```

Optional flags: `--port 8080`, `--host 0.0.0.0`, `--skip-skills`.
First run auto-fetches the site's skill taxonomy into `jobs.db` (skippable with `--skip-skills`).

**Windows, no Python yet?** Double-click `install.bat` once (it makes a venv and installs
the dependencies), then `run.bat` to start.

**Fresh clone, first run:** open **http://127.0.0.1:8371**, expand **Resume → Edit your
resume** and fill in your name, email, phone and skills. Hit **Scrape jobs** (or wait for
the auto-run) to fill the table. Everything you add stays on this machine — your data and
resume are local, only the code is in this repo.

Configuration lives in `config.json`:

| Key | Meaning |
|---|---|
| `base_url` / `api_url` | Site + skills API origin |
| `db_path` | SQLite file (relative → resolved against the project dir) |
| `request_delay` | Global throttle between outbound requests (seconds) |
| `max_retries` | Retries on 429/5xx (exponential backoff; also respects server `Retry-After` headers) |
| `enrich_workers` | Parallel detail-page workers |
| `enrich_interval_days` | "Check for updates" re-checks jobs older than this |
| `backup_retention_days` | How many backups to keep (default 7 if omitted) |

## Workflow

1. **Scrape jobs** — enter a keyword (e.g. `bookkeeper`) and/or pick categories & skills,
   then click **Scrape jobs**. The console streams live: harvest stubs land in the table
   as they're found, then the enrich phase reports progress `[n/N]` and a summary
   (`▸ Enrich: X open, Y closed, Z err`).
2. **Filter the table** — search box, column funnels (each header has a ▼ filter),
   the "With salary" toggle, and header-click sorting. The *Date posted* column sorts
   chronologically / reverse-chronologically and filters by a **From/To date range**.
3. **Keyword auto-hide** — maintain positive (keep) and negative (hide) keyword chips,
   then **Apply**. Positive keywords are an aggressive rule for `New` jobs: every job
   that doesn't contain at least one positive keyword is hidden, and every job that
   does is restored — matched on whatever text exists yet (title, company, skills,
   description once enriched), so fresh harvests are filtered immediately.
   Keywords match **whole words** (case-insensitive, simple plurals included):
   `AI` matches "AI", "AI-powered" — never "email" or "chain"; `video` catches
   "Video Editors" but not "videography". Manually set statuses are never
   clobbered by the filter.
4. **Track** — click any row for the detail modal: change status
   (New → Interested → Applied → …), notes, follow-up date, and view history.
   The green/red/gray dot shows the site-side state (Open / Closed / unknown).
5. **Check for updates** — re-fetches detail pages for jobs whose check is stale
   (or never checked). Deleted listings (HTTP 404/410, "no longer available" wording)
   are marked **Closed**. **Stop** aborts a run mid-flight; the console says how
   far it got.

**Tips**

- Keep keywords narrow — every page/detail page is a real request to the site,
  throttled to 1/second.
- Hidden jobs stay in the DB for dedup — they won't resurface in future scrapes.
- **CSV export** — toolbar button downloads a full server-side CSV dump of all
  jobs (no pagination, streams via `/api/jobs/export`). The client-side JS
  fallback still works for quick one-off exports.

## Auto-run & alerts

The **Auto-run** panel (sidebar) keeps the tracker fresh on its own:

- **Auto-run enabled** + interval (1–24 h, default 4 h) — a daemon thread in the
  server harvests the whole board and re-checks new/stale jobs on schedule.
  Manual *Scrape*/*Check* and auto-run never run at once (shared lock).
- **Enable desktop alerts** — one click for Notification permission; you then get
  a desktop notification for every new-job batch, job closure, salary change, and
  follow-up that's due, delivered over the SSE stream (`/api/events`).

The app starts at Windows logon (`JobHunter` scheduled task → `pythonw main.py`),
and `JobHunter-Backup` runs `scripts/backup.py` daily at 03:00 — a `VACUUM INTO`
snapshot into `backups/`, keeping the last N (from `config.json`
`backup_retention_days`; default 7).

## Data quality

- **Reposts** — same title re-posted by the same employer gets a `↻ repost`
  badge (points at the original). *Hide reposts* toolbar toggle filters them.
- **Salary** — free-text salary is parsed into monthly min/max and shown as a
  `₱…/mo` / `US$…/mo` chip (hourly×160, weekly×4.33, daily×30, annual÷12).
- **Parse watchdog** — if the search page stops yielding job boxes while the
  site claims results, the run logs a `structure change` alert instead of
  silently recording zero.

## Scrape curation, auto-applied filters, full reset
- **Auto-run scope** — the scrape panel's keywords/categories/skills can be
  saved as the auto-run scope (`POST /api/scrape-scope`). The 4-hour auto-run
  then harvests only that scope; empty scope = scrape everything (default).
- **Keyword rules persist + auto-apply** — `Apply auto-hide` saves your
  positive/negative lists server-side. Every auto-run re-applies them to fresh
  jobs automatically (desktop alert when it hides any). Inputs hydrate from
  the saved rules on page load — a refresh no longer wipes your filters.
- **Full reset** — toolbar button: deletes every job + status history in one
  shot. Keeps saved keyword rules, scrape scope, scheduler settings, resume
  masters, and backups.

## Resume tailoring + ATS

- **Master resumes** (`resumes/masters.json`) — multiple named track profiles
  in one file: `clinical-data-automation` (default), `healthcare`, `tech-data`,
  seeded from the real Dropbox masters. Add/rename any via
  `PUT /api/resume {"profile": name, "master": …}`.
- **ATS score** — deterministic, not LLM (recruiter-side first passes are
  keyword + structure based, and a rule scorer can't hallucinate):
  `GET /api/resume/ats?job_id=N[&auto=1]` → 0-100 = skills 40 + keywords 20 +
  format 25 + completeness 15, with matched/missing skills and rule-based
  suggestions. `auto=1` scores every profile and returns the best fit —
  the job drawer uses this, so the right track always gets your resume.
- **ATS filter** — toolbar toggle “ATS ≥ 50” hides every job your best-fitting
  profile scores below 50 on (`GET /api/jobs?min_ats=50`). Your 2,800-row list
  collapsed to the 37 jobs actually worth your time.
- **Tailor** — `POST /api/resume/tailor {"job_id": N, "auto": 1}`: the LLM
  rewrites the best-fitting profile for that job — rewrite & reorder only,
  never invents facts, written in the owner's tone (short, no buzzwords);
  invalid or failed output falls back to the profile. Returns tailored resume + score.
- **Export** — `GET /api/resume/export?job_id=N&tailored=1&fmt=docx|txt[&auto=1]`:
  one column, standard headings, real bullets — the layout ATS parsers chew
  best. PDF skipped (docx is what ATS want).
- **1-page CV (Harvard)** — `POST /api/resume/build {"job_id": N, "auto": 1}`:
  digests every file in `resume_sources` (PDF/DOCX/TXT/MD/JSON, e.g. your
  Dropbox resumes folder), the LLM drafts a RenderCV Harvard-template YAML
  from those facts only, renders it, and **iterates until it is exactly one
  page** (max 4 rounds; it never ships a two-pager). Output PDF + YAML land
  in `resumes/built/` and list in the panel's *Built CVs*. Optional: needs
  Python 3.12+ (`install.bat` makes a separate `.venv-rendercv`; the button
  hides itself when the toolchain is missing).
- **LLM config** — `config.local.json` (gitignored overlay of `config.json`,
  any OpenAI-compatible endpoint): `llm_base_url` / `llm_api_key` /
  `llm_model`. Copy `config.local.json.example` to `config.local.json` and point it at
  your endpoint (a local vLLM box, OpenAI, etc.). Without it, tailor and CV
  build return 503; the ATS scorer works regardless.
- **Resume sources** — `resume_sources: ["C:\\Users\\YOU\\Dropbox\\Resumes"]`
  in `config.local.json`: folders or files of your resume PDFs/DOCX/TXTs that
  the 1-page CV builder digests. Defaults to the project's `resumes/` folder.

## Health check

`GET /health` returns `{"status": "ok", "pid": ...}` — useful for monitoring,
Docker health checks, or Windows Task Manager restart scripts.

## Shutdown (Ctrl-C)

The first **Ctrl-C** sends a stop signal to the in-flight pipeline run (if
any), then uvicorn stops accepting connections and waits for the run's stream
to end — **at most 10 seconds**, after which it forces the shutdown. A
**second Ctrl-C** during shutdown exits immediately.

The process ends with a summary line — where the last run landed, e.g.
`Last run: 2026-02-24 10:00:00 (stopped - partial results kept in jobs.db)`
— and closes its database handles. Stopped runs are recorded as `stopped`
(partial results kept), never `failed`; only a raised error is `failed`.

## Logging

Log files rotate at 5 MB each (3 backups) and land in `job_hunter.log*`. A copy
of INFO+ messages streams to stderr when running interactively.

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point (DB init, skills refresh, uvicorn) |
| `config.json` | Runtime configuration |
| `app/server.py` | FastAPI app: REST + SSE endpoints |
| `app/schemas.py` / `app/sse.py` | Pydantic models / SSE helpers |
| `db/connection.py` | Schema, migrations (version-gated), `get_db()` |
| `db/migrate.py` | One-time data repairs run on version bumps |
| `db/repos/jobs.py` | Job CRUD + query + status history |
| `db/repos/skills.py` | Skill taxonomy store |
| `scraper/client.py` | HTTP client: throttle, backoff, stop signal, respects `Retry-After` headers |
| `scraper/parsers.py` | HTML → structured data (search list, detail, closed detection) |
| `scraper/pipeline.py` | `harvest()` / `enrich()` event generators |
| `scraper/skills.py` | Skills API client |
| `resumes/{schema,ats,tailor,render}.py` | Multi-profile master JSON, deterministic ATS scorer, LLM tailor, docx/txt render |
| `resumes/master.json` | Starter template (edit in the UI) — a fresh clone starts from this |
| `resumes/masters.json` | Your named track profiles (gitignored, local-only) — created by editing the resume in the UI |
| `config.local.json.example` | Template for the (gitignored) `config.local.json` LLM settings |
| `install.bat` / `run.bat` | Windows double-click setup + start (creates a venv) |
| `static/index.html` / `static/app.js` / `static/style.css` | Dashboard UI |
| `tests/` | pytest suite (DB, parsers, pipeline, API) |

## Development

```bash
pip install -r requirements-dev.txt   # pytest + ruff (test/lint gate)
python -m pytest tests/ -q      # full suite, no network
JOBS_DB_PATH=/tmp/sandbox.db python main.py --port 8372   # run against a DB copy
```

`JOBS_DB_PATH` (absolute, or relative to the project dir) overrides `db_path` —
used to test against a copy of a real database.

## License

Copyright (c) 2025–2026 Jeyson Anki. **All rights reserved.** This repository is
private; no open-source license is granted, and the absence of a `LICENSE`
file is deliberate. Do not copy code from here without permission.
