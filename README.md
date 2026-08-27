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

Configuration lives in `config.json`:

| Key | Meaning |
|---|---|
| `base_url` / `api_url` | Site + skills API origin |
| `db_path` | SQLite file (relative → resolved against the project dir) |
| `request_delay` | Global throttle between outbound requests (seconds) |
| `max_retries` | Retries on 429/5xx (exponential backoff) |
| `enrich_workers` | Parallel detail-page workers |
| `enrich_interval_days` | "Check for updates" re-checks jobs older than this |

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
- Export the current view to CSV from the toolbar.

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
| `scraper/client.py` | HTTP client: throttle, backoff, stop signal |
| `scraper/parsers.py` | HTML → structured data (search list, detail, closed detection) |
| `scraper/pipeline.py` | `harvest()` / `enrich()` event generators |
| `scraper/skills.py` | Skills API client |
| `static/index.html` / `static/app.js` / `static/style.css` | Dashboard UI |
| `tests/` | pytest suite (DB, parsers, pipeline, API) |

## Development

```bash
python -m pytest tests/ -q      # full suite, no network
JOBS_DB_PATH=/tmp/sandbox.db python main.py --port 8372   # run against a DB copy
```

`JOBS_DB_PATH` (absolute, or relative to the project dir) overrides `db_path` —
used to test against a copy of a real database.
