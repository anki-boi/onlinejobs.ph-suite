# spec.md — Job Hunter (`onlinejobs.ph-suite`)

**Type:** audit + improvement spec + delegated build plan
**Date:** 2026-09-12
**Baseline commit:** `390870c` (2026-09-08)
**State:** 192 tests pass in 8.4s · 8,938 tracked lines (py/js/html/css) · local DB: 694 jobs, 660 enriched, 54 employers

---

## 0. How to use this document

This is a **decision-ready spec and an execution plan**, not code.

- **The user approves §3 (decision points) and §6 (wave plan).** Everything else is already decided.
- Each task has an ID, files, acceptance criteria, and a verify command. One task = one commit = one reviewable diff — pushed directly to `main` (D9, see §6.5 for what replaces the merge gate).
- Tasks marked **‖** are parallel-safe (no shared files). Tasks marked **→** are serialized on the task before them.
- Every wave ends with a **gate**: tests green, verify commands run, README updated. No wave starts before the previous gate passes.
- **Rule:** no task may be implemented by the planner. Implementation is delegated per §6.4.

---

## 1. Current state

### 1.1 What exists

| Layer | Files | Notes |
|---|---|---|
| Entry | `main.py` | DB init, skills boot, uvicorn, starts scheduler thread |
| API | `app/server.py` (991 lines) | ~35 endpoints, all logic inline |
| Support | `app/{schemas,sse,events,scheduler,pipeline_apply}.py` | serviceable |
| DB | `db/connection.py`, `db/migrate.py`, `db/repos/{jobs,skills,settings}.py` | raw SQL, `row_factory=Row`, WAL |
| Scraper | `scraper/{client,parsers,pipeline,skills,salary}.py` | requests + BeautifulSoup, 1 req/s throttle, SSE event generators |
| Resume | `resumes/{schema,ats,tailor,render,digest,yamlcv}.py` | deterministic ATS scorer + LLM tailor + rendercv 1-page loop |
| UI | `static/{index.html,app.js,style.css}` | 337 + 1,129 + 510 lines, vanilla JS, no build step |
| Tests | `tests/` (16 files, 192 tests) | good unit coverage, no frontend/network/CI |

### 1.2 What is genuinely good (do not regress)

- Deterministic ATS scorer instead of an LLM score (`resumes/ats.py`) — correct call, keep it deterministic.
- `# ponytail:` comments already mark known ceilings — rare discipline, keep the convention.
- SSE pipeline streaming with a shared `pipeline_lock` so manual runs and auto-run can't overlap.
- Schema migrations gated on `PRAGMA user_version` (runs once per DB, not per request).
- The parse watchdog (0 boxes parsed while the site claims N results → loud error) — the single best robustness feature in the repo.
- Backup via `VACUUM INTO` with retention, scheduled task, and a real test file.

### 1.3 What is weak

| Area | Verdict |
|---|---|
| Fresh-install correctness | **Broken** (§2.1) |
| Config plumbing | Config read once at import; two keys silently ignored |
| Server structure | 991-line god module, routes + business logic + caches |
| Caching | Two duplicate ATS caches, one dead, one never invalidates |
| Salary data | Currency-blind: text sort, no currency column, mixed-unit comparisons |
| Frontend scale | Whole table in the DOM (`per_page=99999`), brittle positional selectors |
| Mobile / a11y | No `@media`, 0 `aria-*` |
| OSS hygiene | No CI, no LICENSE, no pinned deps, no `pyproject.toml` |
| Privacy | Author's personal Dropbox path committed to a public repo |
| Product depth | Tracks jobs; does not help win them (no cover letters, interview/offer CRM, analytics) |

---

## 2. Verified defects

All line numbers are against `390870c`. Each defect gets a permanent regression test.

### 2.1 P0 — a fresh clone cannot start

`resumes/digest.py:9` imports `pymupdf` at module top. `app/server.py:38` imports `resumes.digest` unconditionally. `pymupdf` is **not in `requirements.txt`** (which lists only fastapi, uvicorn[standard], requests, beautifulsoup4, python-docx).

> `pip install -r requirements.txt && python main.py` → ImportError on a clean machine.

Same class of bug: `pytest` is not in `requirements.txt` while README documents `python -m pytest`.

**Worse in practice:** `.venv/` does not exist on this machine (only `.venv-rendercv/`). The app is currently running on a global uv-managed Python 3.11.15 — so `run.bat`, the README's primary "double-click to start" path, fails today with `.venv not found - run install.bat first`. The tested path is not the documented path.

**Fix (W1.1):** add `pymupdf`, `pytest`; move optional-import (`digest`, `yamlcv`) behind a lazy import **inside** the route that needs it; add a `pip install -r requirements.txt` smoke job to CI; run `install.bat` end-to-end on a clean tree and prove `run.bat` starts the app.
**Accept:** clean venv → install → `python -c "import app.server"` → exit 0; `run.bat` starts the server.

**Environment confirmed (2026-09-12):** Python 3.12.0 is the default `py` launcher (3.11.15 available via uv) → the CI matrix py3.11/3.12 is realistic. `gh` is authenticated as `anki-boi` with `repo` + `workflow` scopes → CI can be pushed directly. `.venv-rendercv/Scripts/rendercv.exe` exists → W7.6 is feasible. `GET /jobseekers/jobsearch` returns 200 / 188 KB → W3.1 fixture capture is viable without any session/login.

### 2.2 P0 — personal path in public source

`app/server.py:489` — `sources = _cfg.get("resume_sources") or [r"C:\Users\PC\Dropbox\Resumes", str(BASE_RESUMES)]`.
Leaks the author's machine layout to a public repo and hard-codes a path that is meaningless on every other machine.
**Fix (W2.4):** `or [str(BASE_RESUMES)]`; add a CI grep gate for `C:\\Users\\<real-name>` patterns.
**Accept:** `grep -rn "Dropbox" --include=*.py .` returns nothing outside `config.local.json.example`.

### 2.3 P1 — two ATS caches, one dead, one immortal

`app/server.py:76-111` defines `_ats_memo`/`_best_ats_for_row` **and** `_ats_cache`/`_best_for_row`/`_ats_cache_refresh`. Grep proves `_best_for_row` and `_ats_cache_refresh` are **never called** anywhere in `app/` or `tests/`.

The live `_ats_memo` keys on `(MAX(id), COUNT(*), masters mtime)` — its own comment admits in-place enrichment (title/description/skills updated by a re-check) does **not** invalidate it, so `min_ats` can hide a job you just made a better match for, until a row is added or deleted.
**Fix (W2.3):** delete the dead pair; key the cache on `(jobs_version, masters_mtime)` where `jobs_version` is a monotonic counter bumped by every write in `db/repos/jobs.py`.
**Accept:** unit test — enrich a job in place (improve its skills), assert the cached score changes on the next request.

### 2.4 P1 — scheduler ignores two config keys

- `app/scheduler.py:133` — `enrich(client, jobs_to_enrich, workers=3)`. README documents `enrich_workers`. Manual check already sends `workers: null` (`static/app.js:443`), so only auto-run is wrong.
- `app/scheduler.py:120` — `max_age_days=7` hard-coded while `config.json` defines `enrich_interval_days: 7` and README says the key drives "re-check jobs older than this".

**Fix (W2.5):** pass `_cfg` values through `run_once(client, conn, publish, cfg)`; default to the current values when keys are absent.
**Accept:** test — `enrich_workers: 5`, `enrich_interval_days: 3` in cfg → auto-run calls `enrich(..., workers=5)` and `get_jobs_needing_enrichment(..., max_age_days=3)` (asserted via monkeypatch).

### 2.5 P1 — one SQLite connection per request, never closed

`app/server.py:136-147`: `get_db()` returns `dbconn.get_conn()`, which opens a **new** connection (`sqlite3.connect`) on every call. No `.close()` exists anywhere in `app/` or `db/` (verified by grep). It relies on CPython refcount finalization; `PRAGMA journal_mode=WAL` + `foreign_keys=ON` are re-executed per request.
**Fix (W2.1):** `get_db()` becomes a FastAPI dependency: thread-local connection per worker thread, created once, reused, with an explicit `close` on app shutdown.
**Accept:** test — 200 sequential requests leave connection count flat (`sqlite3.connect` monkeypatched to count).

### 2.6 P1 — salary is currency-blind and text-sorted

- `db/connection.py:55-56` has `salary_min`/`salary_max` but **no currency column**.
- `db/repos/jobs.py:41` — `SORTABLE` includes `"salary"`, so sorting is `ORDER BY salary` on free text: `"₱40,000/mo"`, `"US$800/mo"`, `"TBD"` compare lexicographically. Meaningless.
- `scraper/salary.py` already computes the currency — then `db/repos/jobs.py:305-306` throws it away.

Result: a US$800/mo job can outrank a ₱60,000/mo job, and "highest salary" is nonsense.
**Fix (W4.1/W4.2):** add `salary_currency TEXT` + `salary_monthly_min/max` normalized to PHP (config-driven FX rate), sort on the numeric column, expose `sort=salary` → numeric.
**Accept:** fixture test with `["US$800/mo", "₱60,000/mo", "TBD"]` → PHP order `[60000, ~46400, NULL]`; `TBD` rows sink to the bottom in both directions.

### 2.7 P1 — frontend loads everything and selects by position

- `static/app.js:4` — `perPage: 99999`. Fetch + stringify + build DOM for every row. At 694 rows it is slow; at 5,000 it is unusable; at 50,000 it will hang the tab.
- `static/app.js:205-260` `applyVisibleFilter()` reads `row.cells[3]` and `.querySelector('.mt-1')` — a column reorder or class rename silently breaks filtering (no error, wrong rows hidden).
- `static/app.js:~250` — `banner.innerHTML = parts.join(...)` with user-controlled skill/category names. Self-XSS only (localhost), but it is unnecessary: the rest of the file already uses `esc()`.

**Fix (W6.1/W6.2/W6.3):** server-side pagination + virtualized/incremental table; `data-*` attributes for filter fields; `textContent` everywhere.
**Accept:** 5,000-row seeded DB → first paint < 1.5s, scroll stays 60fps, no `innerHTML` with non-escaped values (`grep` gate).

### 2.8 P2 — SSE generators can die silently mid-stream

`app/server.py:619+` (`run_pipeline`) — the `generate()` body only wraps the lock release in `finally`. If `harvest`/`enrich` raises, the client receives a truncated 200 stream with no `error` and no `done`; the UI's `setScraping(false)` never fires because it keys off the `done` event. Same in `run_check` (`:723+`).
**Fix (W2.2):** wrap the loop bodies in `try/except Exception` → `yield sse("error", ...)` then `yield sse("done", "failed")`; release the lock in `finally`.
**Accept:** test injecting an exploding generator → response contains `event: error` **and** `event: done`.

### 2.9 P2 — `pipeline_lock` is per-process

`app/scheduler.py:29` — a `threading.Lock`. Two server processes on the same `jobs.db` (or the documented `JOBS_DB_PATH` sandbox running alongside the real one) each think they hold the only lock, and both scrape OJ.ph at 1 req/s.
**Fix (W2.6):** single-instance guard via DB (row in `app_settings` with pid+mtime, stale-lock expiry) plus keeping the in-process lock.
**Accept:** two processes started against one DB → the second logs "another instance is running the pipeline" instead of scraping.

### 2.10 P2 — dead code in a public repo

- `db/repos/skills.py:42` — `rows = conn.execute(...) if False else []`. Residual scaffolding, immediately overwritten by the Python fallback below it.
- `app/server.py:99-111` — the dead ATS cache above.
**Fix:** W2.3 + a `ruff --select F401,F841` (unused import/assignment) CI gate.
**Accept:** `ruff check --select F401,F841,F811 .` exits 0.

### 2.11 P2 — migrations have no version steps

`db/connection.py:SCHEMA_VERSION = 4`; `db/migrate.py:migrate()` is one unconditional function whose every statement must be individually idempotent — including destructive ones (`UPDATE jobs SET status='Hidden' WHERE status='Closed'`). Adding the first migration that is *not* idempotent (e.g. dropping a column, backfilling a currency with a rate) will corrupt data on the next `init_db()`.
**Fix (W2.7):** ordered step registry `MIGRATIONS: dict[int, callable]`, applied `for v in range(current+1, SCHEMA_VERSION+1)`; keep existing statements as step 4.
**Accept:** test — DB at version 2 migrates only steps 3 and 4; a step that raises leaves `user_version` unchanged.

### 2.12 P2 — keyword filter scans all rows in Python each auto-run

`app/server.py:_apply_keyword_filters` (`:899+`) `SELECT`s every job, regexes it in Python, then batches writes. Fine at 700 rows, ~O(n) per 4h run forever. `_find_repost_origin` (`db/repos/jobs.py:24-38`) is worse: an N+1 query loop over each employer's rows.
**Fix (W4.4):** prefilter in SQL (`WHERE status='New' OR filter_hidden=1`), and match with `LIKE` on a normalized column; store `norm_title` for repost lookup with an index.
**Accept:** 50k-row seeded DB → keyword apply < 2s; repost detection is a single query.

### 2.13 P3 — smaller findings

| # | Finding | Fix |
|---|---|---|
| a | `_cfg` read once at import (`app/server.py:57-62`) — editing `config.local.json` needs a full restart, silently. | W1.4 config service with `POST /api/config/reload` + UI banner |
| b | `/health` (`:149`) returns `pid` only; a locked/corrupt DB reports healthy. | W1.5 add DB ping + `auto_run` last_error + version |
| c | `Stop` (`:753`) sets the flag on the shared client singleton, so one tab's Stop halts every tab and the auto-run. | W2.8 per-run `run_id` + per-run stop flag |
| d | `min_ats` filters **after** pagination (`:228`), so `total` is the page count. UI hides it by fetching everything. | W4.3 compute ATS-filtered totals in one query (SQL-visible score or an `ats_score` cache table) |
| e | Repost badge compares title+employer only; same employer re-posting a *renamed* role is missed; `repost_of` has no unique constraint. | W4.5 similarity threshold + constraint |
| f | No soft delete: `/api/jobs/reset` (`:848`) and any delete is irreversible outside the nightly backup. | W4.6 `deleted_at` + `/api/jobs/trash` + restore |
| g | `harvest()` keyword filter matches title only, lowercased whole-string split on commas — a keyword never matches skills or description. | W3.3 match title+skills, document it |
| h | `scraper/client.py` throttle is exact-1s with no jitter; a fixed cadence is the easiest pattern to fingerprint. | W3.4 jitter ±30% |
| i | `static/index.html` — 16 `<label>` for ~30 inputs, 0 `aria-*`, 0 `@media` queries. | W6.4/W6.5 |
| j | README's "2,800-row list → 37 jobs" and job counts no longer match the shipped DB (694). | W1.6 counts moved to a generated badge or removed |

---

## 3. Decision points (approve or override)

| # | Decision | Default chosen | Why | Cost of being wrong |
|---|---|---|---|---|
| D1 | **Product direction** | Local single-user tool that also reads well as a public repo (current shape) | The code, gitignore, and README already assume this. Multi-user would rewrite auth, storage, and deploy for a user who has one user. | High — changes W5/W8 entirely |
| D2 | **Frontend** | Keep vanilla JS, split `app.js` into ES modules, add server-side pagination | A framework rewrite buys nothing at this scale and costs the no-build-step property. | Medium |
| D3 | **Storage** | SQLite + FTS5, add one `jobs_fts` virtual table for search | Search currently `LIKE '%x%'` over 4 columns; FTS5 is built into the stdlib SQLite and removes the full scan. Postgres is unjustified at 10⁵ rows. | Low |
| D4 | **Salary normalization** | Store original text + `salary_currency` + PHP-normalized monthly min/max; FX rate in config | Lets sorting/filtering be honest without discarding what the employer wrote. | Low |
| D5 | **Auto-apply** | **Never auto-submit an application.** Generate cover letter + prefill, human clicks send | OJ.ph ToS + account-ban risk + a wrong résumé sent is unrecoverable. | High — do not delegate this away |
| D6 | **LLM boundary** | LLM may rewrite/reorder/select only; every deterministic score, fact, and currency conversion is rule-based | Matches existing `resumes/ats.py` philosophy; an LLM score that drifts cannot be trusted or tested. | Medium |
| D7 | **Scope of "go wild"** | W9 features ship behind per-feature flags, off by default | Keeps the trusted core stable while the fun stuff lands | Low |
| D8 | **Licensing** | **All rights reserved — no LICENSE file.** README states it explicitly; drop `CONTRIBUTING.md` (inviting contributions under an all-rights-reserved repo is contradictory); keep `SECURITY.md` | Owner's call (2026-09-12): public as a portfolio/read-only artifact | Low |
| D9 | **Delivery** | **Direct commits to `main`.** Compensating control: `tools/gate.sh` (lint + full test suite) is a `pre-push` hook and CI is a post-hoc alarm, not a gate | Owner's call (2026-09-12): speed over review overhead. See §6.5 | High if the local gate is skipped |

---

## 4. Target architecture

### 4.1 Layout

```
main.py                     # CLI + wiring only (no logic)
app/
  server.py                 # app factory, mounts routers, middleware  (~120 lines)
  routers/                  # jobs.py, pipeline.py, resume.py, settings.py, events.py
  services/                 # jobs_query.py, keywords.py, ats_cache.py, scrape.py, alerts.py
  deps.py                   # get_db (thread-local), get_cfg, get_client, run_guard
  config.py                 # load + validate + reload; typed Accessors, no raw dict gets
  schemas.py  sse.py  events.py  scheduler.py   (kept)
db/
  connection.py             # journal/PRAGMA/thread-local pooling
  migrations/               # v5_salary_currency.py, v6_fts.py, ... + registry
  repos/                    # + runs.py (run history), alerts.py
scraper/
  client.py  parsers.py  pipeline.py  skills.py  salary.py   (kept)
  fixtures/                 # saved HTML corpus for parser tests
resumes/                    # kept; digest/yamlcv become lazily imported
static/js/                  # app.js split: api.js, table.js, filters.js, resume.js, sse.js
tools/                      # dev-only scripts (smoke_e2e.py, seed_fixtures.py)
```

### 4.2 Layering rules (enforced by review, not tooling)

1. Routers: HTTP only — parse, call one service, map errors. No SQL, no regex, no caching.
2. Services: business logic, no `Request`/`Response`.
3. Repos: SQL only, return `Row`s. No cross-repo calls.
4. `parsers.py` stays pure (no I/O) so fixtures are cheap.

### 4.3 Schema changes (additive only)

| Version | Change |
|---|---|
| v5 | `jobs.salary_currency TEXT`, `jobs.salary_monthly_min REAL`, `jobs.salary_monthly_max REAL`, `jobs.norm_title TEXT`, `jobs.deleted_at TEXT`; indexes on `(salary_monthly_min)`, `(norm_title, employer_id)`, `(deleted_at)` |
| v6 | `jobs_fts` (FTS5, content=`jobs`, external content) + triggers on insert/update/delete; `repost_hash TEXT UNIQUE` (nullable) |
| v7 | `pipeline_runs(id, kind, started_at, finished_at, inserted, enriched, closed, errors, status, summary_json)` |
| v8 | `alert_rules(id, type, enabled, threshold_json)` + `alert_log(rule_id, fired_at, payload)` |

Every step is idempotent-by-construction through the §2.11 registry — that is the point of W2.7 landing first.

---

## 5. Workstreams

Effort is in focus-hours for one delegated agent. `‖` = parallel-safe against its wave-mates; `→` = blocked on the listed task.

---

### W1 — Foundation, setup correctness, OSS hygiene

| ID | Task | Files | Acceptance | Verify | Eff |
|---|---|---|---|---|---|
| W1.1 | Fix fresh-install: add `pymupdf`, `pytest`; lazily import `resumes.digest`/`yamlcv` inside their routes; split `requirements.txt` (runtime) + `requirements-dev.txt`; add `pyproject.toml` with `requires-python >=3.11`, ruff, pytest config | `requirements*.txt`, `pyproject.toml`, `app/server.py` | clean venv boots; `import app.server` works without pymupdf | `python -m venv /tmp/v && /tmp/v/bin/pip install -r requirements.txt && /tmp/v/bin/python -c "import app.server"` | 2 |
| W1.2 | CI (`‖`): GitHub Actions — matrix py3.11/3.12, `ruff check`, `pytest -q`, plus a `pip install` smoke job. Runs on push to `main` and on PRs. **With direct-to-main this is an alarm, not a gate — see §6.5** | `.github/workflows/ci.yml` | green on push | `gh run watch` | 2 |
| W1.3 | Repo hygiene (`‖`): **no LICENSE file** (D8) + an explicit all-rights-reserved section in README, `SECURITY.md`, issue templates, `.editorconfig`, `ruff` config. **`tools/gate.sh` + `pre-push` git hook are REQUIRED here** — they are the only thing standing between a bad commit and `main` under D9 | repo root, `tools/gate.sh`, `.githooks/pre-push` | `tools/gate.sh` runs lint + full suite and exits non-zero on failure; hook is installed via `git config core.hooksPath .githooks` | `tools/gate.sh && echo PASS` | 3 |
| W1.4 | Typed config service: `app/config.py` with schema validation, defaults, `reload()`, `GET /api/config` (secrets redacted), `POST /api/config/reload`; UI banner showing which optional features are off | `app/config.py`, `app/server.py`, `static/*` | editing `config.local.json` + reload changes LLM model without restart | test: write cfg → reload → `/api/resume/tailor` uses new model | 4 |
| W1.5 | Real health endpoint (`→` W1.4): `{status, version, db: {ok, path, jobs}, scheduler: {enabled,last_run,last_error}, llm: configured}`; `status:"degraded"` if DB ping fails or `last_error` non-empty | `app/server.py` | corrupt/locked DB → 503 + `degraded` | test with a chmod-000 DB file | 2 |
| W1.6 | Docs truth pass (`‖`): delete stale counts from README, document every config key, add `docs/architecture.md`, `docs/operations.md` (backup/restore/autostart), `docs/scraping.md` (politeness + ToS) | `README.md`, `docs/*` | no unverifiable number in README; every key in `config.json` appears in the table | `python tools/check_readme.py` (new, 20 lines) | 3 |
| W1.7 | Structured logging + request ids + `/api/metrics` (`‖`): JSON log option, `run_id` on every pipeline log line, counters for requests/runs/errors | `main.py`, `app/*` | log lines carry `run_id`; metrics endpoint returns counters | grep a run's lines by id | 3 |
| W1.8 | Dev smoke script `tools/smoke_e2e.py`: boot on a temp DB, hit every endpoint, assert 200/4xx contracts, exit non-zero on failure | `tools/smoke_e2e.py` | runs in CI as a job | `python tools/smoke_e2e.py` | 3 |

**Gate W1:** clean-machine install works; CI green; `/health` reflects reality; README truthful.

---

### W2 — Correctness: connections, caches, locks, migrations

| ID | Task | Files | Acceptance | Verify | Eff |
|---|---|---|---|---|---|
| W2.1 | DB lifecycle (`‖`): thread-local connection pool + `close()` on shutdown; `get_db()` as a dependency; keep `JOBS_DB_PATH` override | `db/connection.py`, `app/deps.py`, `app/server.py` | §2.5 acceptance | connection-count test | 4 |
| W2.2 | SSE hardening (`‖`): wrap every streaming generator in `try/except` → `error` + `done`; never 200-and-truncate; add `run_id` to every event | `app/server.py`, `app/sse.py` | §2.8 acceptance | injected-failure test | 3 |
| W2.3 | Delete dead ATS caches; one `AtsCache` service with explicit `invalidate(scope)`; bump a `jobs_version` counter on every repo write | `app/server.py`, `app/services/ats_cache.py`, `db/repos/jobs.py` | §2.3 acceptance | enrichment-invalidation test | 4 |
| W2.4 | Remove the personal Dropbox path; add a CI grep gate for `C:\\Users\\<name>` / `Dropbox` in tracked source | `app/server.py`, `.github/workflows/ci.yml` | §2.2 acceptance | grep gate | 1 |
| W2.5 | Scheduler honors `enrich_workers` + `enrich_interval_days`; thread `cfg` through `run_once` | `app/scheduler.py` | §2.4 acceptance | monkeypatch test | 2 |
| W2.6 | Cross-process single-flight guard: `app_settings` row with `pid`/`heartbeat_at`, stale after 5 min; surfaced in `/health` and the UI | `app/scheduler.py`, `db/repos/settings.py` | two processes → second refuses to scrape | 2-process integration test | 4 |
| W2.7 | Migration registry: ordered `MIGRATIONS` dict, per-step logging, `user_version` bumped only on success, dry-run mode `python -m db.migrate --dry-run` | `db/connection.py`, `db/migrate.py`, `db/migrations/*` | §2.11 acceptance | version-2 fixture test | 4 |
| W2.8 | Per-run stop: `run_id` + a stop flag per run in the service layer; `/api/pipeline/stop` takes an optional `run_id`; UI sends its own | `app/*`, `static/app.js` | Stop in tab A does not stop tab B's run | 2-client test | 3 |
| W2.9 | Graceful shutdown: SIGINT → stop accepting, signal in-flight runs, wait ≤10s, close DB; document Ctrl-C behaviour | `main.py`, `app/*` | Ctrl-C mid-scrape exits cleanly with a summary line | manual + test | 3 |

**Gate W2:** the four P1 defects have regression tests; `ruff` clean; adversarial review pass on W2.1/W2.6/W2.7 (use `adversarial-review-loop` skill — concurrency + migration code is exactly where silent corruption lives).

---

### W3 — Scraper robustness

| ID | Task | Files | Acceptance | Verify | Eff |
|---|---|---|---|---|---|
| W3.1 | HTML fixture corpus (`‖`): save real search + detail + closed + 404 pages into `scraper/fixtures/`; parser tests run against them; a `tools/check_fixtures.py` warns when a live page no longer matches any fixture selector | `scraper/fixtures/*`, `tests/test_parsers.py` | parser tests never touch the network; drift check exits non-zero on mismatch | `python tools/check_fixtures.py` | 4 |
| W3.2 | Selector table (`→` W3.1): all CSS selectors in one dict with a documented fallback chain per field; a missing field degrades to `None` + a counter, never a silent empty job | `scraper/parsers.py` | a page with the old markup still parses ≥80% of fields | fixture with old markup | 4 |
| W3.3 | Harvest filters (`‖`): keyword matches title **and** skills; `posted_since` applied consistently; log how many were filtered by each rule; unit-test the OR-semantics path | `scraper/pipeline.py` | §2.13g acceptance | filter unit tests | 3 |
| W3.4 | Politeness (`‖`): jitter ±30% on throttle; honor `Retry-After` on 403 too; cap total requests per run (config `max_requests_per_run`); back off harder after 3 consecutive 429s | `scraper/client.py` | no two delays equal; run stops at the cap with a clear log | test with a fake clock | 3 |
| W3.5 | Resumable runs: persist harvest page cursor + next job to enrich in `pipeline_runs`; an interrupted run resumes instead of restarting the whole board | `scraper/pipeline.py`, `db/repos/runs.py` | kill mid-run → restart resumes at the stored cursor | integration test | 5 |
| W3.6 | Login/session support (optional flag): load cookies from a gitignored `cookies.json`; never log credentials; document why (some listings need a session) | `scraper/client.py`, docs | with cookies, a session-only page parses; without, behaviour unchanged | manual + unit | 3 |
| W3.7 | Detail-page completeness scoring: a job whose detail page yielded < 2 of {title, description, skills, salary} is flagged `needs_review` and surfaced in a UI filter | `scraper/pipeline.py`, `db/repos/jobs.py`, UI | flag appears in table + API | fixture test | 3 |

**Gate W3:** parser suite green offline; a deliberately broken fixture flips the drift check red; no unbounded request volume possible.

---

### W4 — Data model, search, salary, dedupe

| ID | Task | Files | Acceptance | Verify | Eff |
|---|---|---|---|---|---|
| W4.1 | Salary v5 migration: `salary_currency`, `salary_monthly_*`, FX config (`fx_to_php.usd`), backfill from existing `salary` text, keep the raw string | `db/migrations/v5_*.py`, `scraper/salary.py`, `db/repos/jobs.py` | §2.6 acceptance | fixture table of 15 real salary strings | 4 |
| W4.2 | Sorting/filtering on numbers: `sort=salary` → `salary_monthly_max`; new filters `salary_min_monthly`/`salary_max_monthly` + currency; UI shows currency chip and an FX-rate tooltip | `db/repos/jobs.py`, `static/*` | sorting is monotonic on a mixed-currency fixture | API test | 4 |
| W4.3 | `min_ats` at SQL level: materialized `ats_scores(job_id, profile, total, updated_at)` refreshed on job/masters change; `min_ats` becomes a JOIN so `total` is truthful | `db/migrations/*`, `app/services/ats_cache.py`, `db/repos/jobs.py` | `min_ats=50` with `per_page=50` returns a correct global `total` | API test | 5 |
| W4.4 | FTS5 search (`→` W2.7): `jobs_fts` over title/company/description/skills; `/api/jobs?q=` uses it with snippet highlighting; keep `search=` as the legacy substring path | `db/migrations/v6_*.py`, `db/repos/jobs.py`, UI | 50k rows: search < 50ms; results ranked | perf test + fuzz test on punctuation input | 5 |
| W4.5 | Dedupe/merge: `norm_title` + `repost_hash`; a `/api/jobs/duplicates` report; merges keep history and notes and are fully reversible | `db/repos/jobs.py`, `app/routers/jobs.py` | §2.13e/§2.12 acceptance | duplicate fixture test | 4 |
| W4.6 | Soft delete + trash: `deleted_at`, `/api/jobs/trash`, restore, purge-after-N-days, `/api/jobs/reset` keeps a trash snapshot instead of destroying | `db/*`, `app/*`, UI | delete → undo works; reset is recoverable | API test | 3 |
| W4.7 | Query perf pass: `EXPLAIN QUERY PLAN` on every list filter; add the missing composite indexes; document the index rationale | `db/migrations/*`, docs | no full scan on the main list query | `EXPLAIN` snapshot test | 3 |

**Gate W4:** salary sort is numerically correct in both directions; search is fast and ranked; destructive actions recoverable.

---

### W5 — API structure

| ID | Task | Files | Acceptance | Eff |
|---|---|---|---|---|
| W5.1 | Route groups → `app/routers/`; server.py = composition root | ✅ e4f3916 |
| W5.2 | Uniform error envelope `{"error":{code,message,detail}}` | ✅ e4f3916 |
| W5.3 | Pagination contract on GET /api/jobs | ✅ 3481803 |
| W5.4 | ETag/304 on GET /api/jobs; idempotent pipeline runs | ✅ 39cda86 |
| W5.5 | OpenAPI: per-group tags + route summaries | ✅ a1c8a6b |
| W5.2 | Uniform error envelope `{error:{code,message,detail}}`, exception handlers for `ValueError`/`sqlite3.OperationalError`/`ScrapeStopped`, and `422` validation messages that name the field | `app/*` | golden tests for each error class | 4 |
| W5.3 | Pagination contract: `page`, `per_page` (cap 500), `total`, `next_cursor`; reject oversize `per_page` with 400 instead of silently accepting 99999 | `app/*`, UI | `per_page=99999` → 400 | 3 |
| W5.4 | Idempotency + ETag for GET lists; `If-None-Match` → 304; `POST /api/pipeline/run` returns a `run_id` usable for status polling without SSE | `app/*` | 304 on repeat; poll endpoint returns the same run | 4 |
| W5.5 | OpenAPI cleanup: tags, summaries, examples, response models; serve `/docs` fully typed | `app/*` | `/docs` shows typed responses, no `{}` blobs | 3 |

**Gate W5:** identical external behaviour except documented contract changes; file sizes sane (no route module > 250 lines).

---

### W6 — Frontend: scale, durability, accessibility

| ID | Task | Files | Acceptance | Eff |
|---|---|---|---|---|
| W6.1 | `app.js` → ES modules (`core/stats/jobs/resume/run/filters` + entry) | ✅ 0671d75 |
| W6.2 | Server-side pagination UI (pager, per_page 50) | ✅ 0671d75 |
| W6.3 | Virtual scroll for >80-row pages (spacer rows) | ✅ 0671d75 |
| W6.2 | Server-side pagination + virtual scroll; `per_page` 100; sticky header; row count from `total`, not DOM | `static/js/table.js`, `app/routers/jobs.py` | §2.7 acceptance; 5,000 rows first paint < 1.5s | 6 |
| W6.3 | Replace positional selectors with `data-field` attributes; remove every non-escaped `innerHTML`; add a grep gate | `static/js/*` | `grep -n "innerHTML" static/js` shows only static template strings | 3 |
| W6.4 | Accessibility: `<th scope>`, `aria-sort`, `aria-live` on the log/toast, focus trap + `Esc` in the detail drawer, real `<label for>`, 4.5:1 contrast check | `static/*` | axe-core scan: 0 critical; full keyboard-only job triage possible | 5 |
| W6.5 | Responsive: `@media` breakpoints, sidebar collapses to a drawer, table becomes a card list on narrow screens, tap targets ≥ 40px | `static/style.css` | usable at 390px width; no horizontal scroll | 5 |
| W6.6 | URL state: filters/sort/page/skill chips in the query string; a shareable/bookmarkable view; back button works | `static/js/*` | copy URL → same view in a new tab | 3 |
| W6.7 | Keyboard triage flow: `j/k` navigate, `1..8` set status, `s` open drawer, `?` shortcut sheet; optimistic update + rollback on failure | `static/js/*` | triage 20 jobs without a mouse; failed PATCH reverts the row | 4 |
| W6.8 | Offline/degraded behaviour: SSE reconnect with backoff + a visible "reconnecting" state; queued mutations while disconnected; a stale-data timestamp | `static/js/sse.js` | kill the server → banner appears → restart → stream resumes | 3 |
| W6.9 | Frontend tests: Playwright smoke (boot, filter, open job, change status, export) running against a seeded temp DB in CI | `tests/e2e/*`, `.github/workflows/ci.yml` | e2e job green; catches the §2.7 class of regression | 6 |

**Gate W6:** axe clean, mobile usable, e2e green, no positional selectors left.

---

### W7 — Resume + ATS pipeline

| ID | Task | Files | Acceptance | Eff |
|---|---|---|---|---|
| W7.1 | Single source of truth for resume text: today `ats.py:resume_to_text`, `render.to_txt`, and the YAML builder each format independently — one renderer, three consumers | `resumes/render.py`, `resumes/ats.py` | ATS score identical before/after refactor (golden test) | 4 |
| W7.2 | ATS explainability: per-rule breakdown with weights, matched/missing terms, and "what to add to gain N points" — deterministic, no LLM | `resumes/ats.py`, UI | golden fixture with hand-computed expected score | 4 |
| W7.3 | Tailor review diff: show added/changed/removed lines vs the master, **block** the export if any number/employer/date changed (hallucination guard) | `resumes/tailor.py`, `app/routers/resume.py`, UI | a tampered LLM output is rejected with a visible diff | 5 |
| W7.4 | Cover letter generator: same fact-locked prompt, same `resume_to_text` source, docx+txt export, human review required | `resumes/coverletter.py`, UI | generated letter contains no fact absent from the corpus | 4 |
| W7.5 | Masters versioning: every `PUT /api/resume` stores a version; `/api/resume/history` + one-click revert | `resumes/schema.py`, `db/migrations/*` | edit → revert restores the previous text byte-for-byte | 3 |
| W7.6 | PDF export option (`reportlab` or the existing rendercv venv) for both resume and letter, with a real one-page overflow check | `resumes/render.py` | exported PDF is 1 page for the fixture résumé | 4 |
| W7.7 | Cover letter / résumé cache keyed by (job_id, profile, masters_version) so re-opening a job does not re-call the LLM | `app/services/*` | second call is instant, no LLM hit (asserted) | 2 |

**Gate W7:** ATS scores unchanged through the refactor; hallucination guard provably blocks a mutated resume. Adversarial review pass (a wrong résumé costs a real interview).

---

### W8 — Automation, alerts, observability

| ID | Task | Files | Acceptance | Eff |
|---|---|---|---|---|
| W8.1 | `pipeline_runs` history: every run (manual/auto) recorded; `/api/runs` + a UI "Runs" panel with duration, inserted/enriched/closed/errors | `db/migrations/v7_*.py`, `app/*`, UI | every run visible; failures visible with the error | 5 |
| W8.2 | Alert rules engine: per-type toggle + threshold (`new_jobs`, `jobs_closed`, `salary`, `follow_up`, `error_streak`, `zero_results`), persisted, quiet hours | `app/services/alerts.py`, `db/migrations/v8_*.py`, UI | repeated conditions don't spam; quiet hours respected | 5 |
| W8.3 | Failure escalation: 3 consecutive failed runs → a loud alert + pause auto-run + a `/health` degraded flag; a "why did nothing happen" diagnostic in the UI | `app/scheduler.py`, `app/*` | simulated failures pause the scheduler with a stated reason | 3 |
| W8.4 | Daily digest: one desktop notification (and optional email via the user's existing `email-mailbox-ops` setup) with new-jobs-by-fit, follow-ups due, closures | `app/services/digest.py` | digest arrives once per day at a configured hour | 4 |
| W8.5 | Scheduled-task self-healing: `scripts/make_autostart.bat` verifies and repairs the task; `/health` reports whether the task exists (via `schtasks /query`) | `scripts/*`, `app/*` | deleting the task then restarting re-creates it | 3 |
| W8.6 | Retention + size guard: cap DB growth (archive jobs closed > N days to a separate SQLite file), report file sizes in the UI | `scripts/*`, `app/*` | 1-year-old closed jobs archived, DB stays < 50 MB | 4 |

**Gate W8:** a 24h unattended run produces exactly one digest, zero duplicate alerts, and a readable run history.

---

### W9 — Product features ("go wild", all behind flags, off by default)

Ranked by value/effort. Ship top-down; stop whenever the user says stop.

| ID | Feature | Why it matters | Depends | Eff |
|---|---|---|---|---|
| W9.1 | **Application CRM**: per-job timeline (applied → reply → interview → offer), interview records with notes and outcomes, offer comparison (salary normalized to PHP), reminder offsets per stage | Turns a job list into a pipeline. Highest value/effort ratio in this section. | W4.1, W8.1 | 8 |
| W9.2 | **Match explainer**: one panel per job — why it scored what it scored, what to change, which profile fits, which of your real achievements map to each required skill | The ATS score already exists but is opaque; making it actionable is the actual product. | W7.2 | 5 |
| W9.3 | **Market analytics**: skills in demand (week-over-week), salary distribution by skill/category (PHP-normalized), hiring velocity per employer, "skills you lack that appear in 80% of your matches" | Turns the scraped corpus into advice; data is already collected and currently unused. | W4.1, W4.4 | 6 |
| W9.4 | **Saved searches / smart views**: named scope + filter + notifier ("remote PM, ≥ ₱60k, posted this week") | The current keyword/scope chips are one global set; saved views make multi-track job hunting sane. | W6.6, W8.2 | 4 |
| W9.5 | **Cover letter + proposal generator** (W7.4) surfaced per job with a review diff | Directly converts to interviews. | W7.4 | (3) |
| W9.6 | **Employer dossier**: every job, note, application, and reply for one employer on one page, with reputation flags (repeat poster, repost count, closure rate) | Scam/lowball detection without extra scraping — computed from the existing DB. | W4.5 | 4 |
| W9.7 | **Application autofill helper**: a Tampermonkey script that pre-fills the OJ.ph application form from the master profile and attaches the tailored docx. **Never auto-submits.** | Removes the worst 10 minutes per application while keeping the human in control. Uses the user's existing userscript workflow (`tampermonkey-userscripts` skill). | W7.4 | 5 |
| W9.8 | **Weekly report**: generated markdown/HTML summary (market + pipeline + next actions) saved to the vault | Good for review and for showing the system works. | W9.3, W8.1 | 3 |
| W9.9 | **Interview prep pack**: for a shortlisted job — likely questions from the JD's skills, your mapped STAR bullets, gaps to prepare answers for | High value at a critical moment, moderate cost, LLM-fact-locked. | W9.2 | 4 |
| W9.10 | **Multi-profile routing**: per-profile filters so a job is matched to the right résumé automatically and out-of-fit jobs are hidden | Already partially present (`best_profile_for_job`); make it explicit and tunable. | W7.2 | 3 |

---

### W10 — Testing, quality gates, performance

| ID | Task | Acceptance | Eff |
|---|---|---|---|
| W10.1 | Coverage gate: `pytest --cov` with a per-package floor (scraper 85%, db 90%, app 75%), enforced in CI | CI fails below the floor | 3 |
| W10.2 | Golden-file tests for ATS scoring, salary parsing, and YAML CV rounds — any drift is a failing diff, not a quiet change | 20+ fixture cases | 4 |
| W10.3 | Property/fuzz tests: salary parser and keyword matcher on random/adversarial strings (the two places where a bad regex silently misclassifies money and jobs) | no crash, no false "AI" match inside "email" | 3 |
| W10.4 | Concurrency tests: parallel requests during a pipeline run; two processes on one DB; SSE subscriber churn | no `database is locked`, no lost events | 4 |
| W10.5 | Performance budget in CI: seeded 50k-row DB — list query < 150 ms, search < 80 ms, keyword apply < 2 s, first paint < 1.5 s | budget enforced, not aspirational | 4 |
| W10.6 | E2E nightly: boot the real app against a **fixture-backed** scraper (no live OJ.ph) and run the full user journey | nightly green | 5 |
| W10.7 | Restore drill: `scripts/restore.py` + a documented drill that restores a backup into a temp DB and asserts row counts | drill passes; documented in `docs/operations.md` | 3 |

---

### 6.5 Direct-to-main: what replaces the merge gate (D9)

With no PR step, nothing reviews a diff before it is on `main`. That is an acceptable trade only if the *automated* half of the gate is local and mandatory:

1. **`tools/gate.sh`** — one script, no flags: `ruff check .` → `python -m pytest tests/ -q` → `python tools/smoke_e2e.py` → `python tools/check_fixtures.py` (once W3.1 exists). Exits non-zero on the first failure. This is the gate; CI just re-runs it on push.
2. **`pre-push` hook** (`.githooks/pre-push`) calling that script, installed in W1.3. `git config --local core.hooksPath .githooks`, so it is repo-local and travels with the clone.
3. **`main` must always boot.** Any task touching `main.py`, `app/`, `db/`, or `static/` is not "done" until `python main.py` starts against a copy of the real DB. W1.8's smoke script covers this in the gate.
4. **Wave boundaries are tags.** `git tag wave-1-stabilize` on the last green commit of each wave, so a bad wave can be reverted as a unit (`git revert --no-commit wave-1-start..wave-1-end`).
5. **Destructive or money-touching tasks get the adversarial review anyway.** D9 removes the PR step, not the review requirement in §6.3 item 7 — for W2.1, W2.6, W2.7, W4.1, W4.6, W7.3, run the review *before* pushing, on the local diff.

---

## 6. Execution plan

### 6.1 Wave plan

Waves are ordered by *risk reduction per hour*. Nothing in W9 starts before W1+W2 gates pass.

| Wave | Contents | Parallelism | Exit gate |
|---|---|---|---|
| **0. Unblock** (day 1) | W1.1, W2.4 | 2 agents | fresh install boots; no personal path; `tools/gate.sh` exists and passes |
| **1. Stabilize** (days 2–5) | W2.1–W2.9, W1.2, W1.3, W1.5, W1.6 | 3 agents (W2.1/W2.2/W2.3 ‖; then W2.6/W2.7 ‖) | all P1 defects fixed with regression tests; `tools/gate.sh` green before every push; CI green; adversarial review passed |
| **2. Correct data** (days 6–10) | W4.1, W4.2, W4.3, W4.7, W3.1, W3.2 | 3 agents | salary sorting/ATS totals are numerically true |
| **3. Refactor for speed** (days 11–15) | W5.1–W5.4, W6.1–W6.3, W4.4, W4.5 | 3 agents (backend/backend/frontend) | same tests green; UI handles 5k rows |
| **4. Reach** (days 16–22) | W6.4, W6.5, W6.6, W6.7, W3.3–W3.7, W8.1, W8.2 | 4 agents | mobile + keyboard usable; run history + alerts live |
| **5. Product** (day 23+) | W7.* then W9.1 → W9.2 → W9.3 → rest | 2 agents | flags on/off cleanly; each feature has a golden test |
| **6. Continuous** | W10.*, W1.7, W1.8, W8.3–W8.6 | ongoing | budgets and gates enforced in CI |

### 6.2 Hard sequencing constraints

- W2.7 (migration registry) **before** any W4/W7/W8 schema change — otherwise the first non-idempotent migration corrupts data on every boot.
- W2.1 (DB lifecycle) **before** W6.2 (pagination) — connection churn + heavy paging is a lock storm.
- W5.1 (router split) **before** W9.* — new features must not extend a 991-line module.
- W7.1 (one resume renderer) **before** W7.2/W7.4 — otherwise the scorer, the letter, and the PDF disagree about facts.
- W4.1 (currency) **before** W9.1/W9.3 offer comparison and salary analytics — otherwise every money comparison is a lie.

### 6.3 Definition of done (every task)

1. Tests written first (RED), then implemented (GREEN) — `test-driven-development` skill.
2. `python -m pytest tests/ -q` green; warning count not increased.
3. `ruff check .` clean.
4. The task's acceptance test exists and would fail on the pre-fix code (state the proof in the PR).
5. Docs touched if behaviour changed (README/config table/OpenAPI).
6. One commit, conventional message, scoped to the task ID. **Committed and pushed directly to `main` (D9) — so `tools/gate.sh` must pass first, every time. Never push a task whose gate is red; there is no merge step to catch it.**
7. Money/security/concurrency/concurrency-adjacent diffs (W2.1, W2.6, W2.7, W4.1, W7.3) → `adversarial-review-loop` (2 consecutive PASSes before merge).
8. No new dependency without a one-line justification in the PR body. Stdlib first (`ponytail` ladder).

### 6.4 Delegation briefs

**Applies to every brief below (D9):** work on `main`. Before every commit, run `tools/gate.sh` and do not commit or push if it fails. CI is an alarm that tells you afterwards that you were wrong — it will not stop you. If a task's gate cannot pass, fix it or revert the commit; do not push a red wave.

Each agent receives: the repo, this file, the task IDs, and the brief below. Agents must not touch files outside their task's file list.

**Brief A — Backend correctness agent**
> Implement W2.1–W2.5, W2.8, W2.9 in order. Read `app/server.py`, `db/connection.py`, `app/scheduler.py` fully before editing. For each task: failing test first, minimal diff, no unrelated refactors, no new dependencies. Report per task: task ID, files, the test that proves the fix, and the command to reproduce it. Stop and ask if a fix requires changing the DB schema (that is W2.7's job).

**Brief B — Data/migration agent**
> Implement W2.7 first (migration registry) with a version-2 fixture DB test, then W4.1, W4.2, W4.4, W4.5, W4.7, W4.3, W4.6. Never reuse a `user_version` number. Every migration must run twice safely and be tested for that. Use real salary strings from the live DB as fixtures (copy the DB to `/tmp` first; never mutate `jobs.db`). Report the before/after `EXPLAIN QUERY PLAN` for the main list query.

**Brief C — Frontend agent**
> Implement W6.1–W6.3, then W6.6, W6.4, W6.5, W6.7, W6.8. Preserve the current look (Calm light palette, Inter/Syne) — this is a durability and scale change, not a redesign. No build step, no framework. Every DOM hook becomes `data-*`. Playwright smoke test required for the triage flow. Report a before/after first-paint time on a 5,000-row seeded DB.

**Brief D — Scraper agent**
> Implement W3.1, W3.2, then W3.3–W3.7. Fixtures only — never add a test that hits the network. Keep the existing parse-watchdog behaviour and extend it. The 1 req/s politeness budget is a hard constraint: any change must be provably unable to exceed it. Report fixture coverage per field per page type.

**Brief E — Product agent** (only after wave 3)
> Implement W7.1, W7.3, W7.2, W7.4–W7.7, then W9.1, W9.2, W9.3, then W9.4/W9.6/W9.7 by value. Every generated document must be fact-checked against the source corpus; export is blocked on any invented number, employer, or date. Every feature ships behind a flag defaulting to off. D5 stands: no auto-submitted applications, ever.

---

## 7. Quality bar (non-negotiable)

- **Input validation at every trust boundary** — the scraped HTML and the LLM output are both untrusted. Never `eval`, never unvalidated `json.loads` into a write path.
- **Money is never approximate.** Currency, FX rate, and the normalization timestamp are stored together; the UI says what rate it used.
- **No silent data loss.** Destructive paths (reset, merge, purge, migrate) are reversible or refuse to run; W2.7 and W4.6 exist for this.
- **No silent failure.** Every run ends with a recorded status; every generator ends with `done`; every parser returns a counter for "field not found".
- **Accessibility is not a phase.** Any new UI control ships with a label, keyboard access, and an `aria` state.
- **Politeness is a feature.** 1 req/s is the ceiling. Backoff, jitter, request caps, and a documented ToS position (`docs/scraping.md`).

---

## 8. Explicitly rejected / deferred (do NOT build)

Recorded so nobody re-proposes them. Each has a trigger that would change the answer.

| Rejected | Why | Revisit if |
|---|---|---|
| Postgres/MySQL | SQLite + FTS5 covers ≥10⁵ jobs; a second service to operate for zero benefit | >1M rows or concurrent writers |
| React/Vue/Svelte rewrite | The UI's problems are DOM size and selector durability, not reactivity. A framework costs the no-build property and rewrites 1,129 working lines | multi-user, or the UI grows past ~5 screens |
| Auth / multi-user / hosted SaaS | One user, one machine, localhost. It adds credential storage, abuse surface, and ops | a second real user appears |
| Docker/K8s/microservices | One process on one PC; `install.bat` + a scheduled task is the correct deploy story | hosted deployment |
| Redis/Celery/task queue | A daemon thread + `pipeline_lock` is sufficient for one 4-hourly job | concurrent multi-source scraping |
| LLM-based ATS scoring | Non-deterministic, untestable, and drifts — `ats.py` is rule-based for exactly this reason (D6) | never |
| Vector DB / RAG over job descriptions | FTS5 + deterministic skill overlap already answers "does this fit"; embeddings add a model, an index, and a dependency | fuzzy semantic matching demonstrably beats FTS on real queries |
| Auto-submitting applications | ToS, account-ban risk, and an unreviewed bad application is unrecoverable (D5) | never |
| pandas/numpy for analytics | `sqlite3` + `GROUP BY` + 30 lines of Python is enough for 10⁵ rows; two heavyweight deps for nothing | statistical modelling is actually needed |
| GraphQL | REST + OpenAPI is already generated and consumed by one client | a second, independent client appears |
| Electron/Tauri desktop wrapper | The browser is the app; a wrapper is packaging with no user benefit | offline-first requirement |
| "AI agent that applies for you" | Same as auto-submit, plus hallucinated facts in a sent document (D6) | never |
| Rewriting `plans/` into the repo | Operational history, not product code; it is correctly gitignored | the repo goes multi-contributor |

---

## Appendix A — Tracked file inventory (grouped by workstream)

| Workstream | Files |
|---|---|
| W1 | `requirements.txt`, `pyproject.toml` (new), `.github/workflows/ci.yml` (new), `SECURITY.md` (new), **no `LICENSE`** (D8), `tools/gate.sh` + `.githooks/pre-push` (new), `app/config.py` (new), `main.py`, `README.md`, `docs/*` (new), `tools/*` (new) |
| W2 | `app/server.py`, `app/deps.py` (new), `app/services/ats_cache.py` (new), `db/connection.py`, `db/migrate.py`, `db/migrations/*` (new), `app/scheduler.py`, `db/repos/settings.py` |
| W3 | `scraper/{client,parsers,pipeline}.py`, `scraper/fixtures/*` (new) |
| W4 | `db/repos/jobs.py`, `scraper/salary.py`, `db/migrations/v5..v6` |
| W5 | `app/routers/*`, `app/services/*`, `app/schemas.py` |
| W6 | `static/js/*` (new modules), `static/index.html`, `static/style.css`, `tests/e2e/*` (new) |
| W7 | `resumes/{ats,tailor,render,schema,digest,yamlcv}.py`, `resumes/coverletter.py` (new) |
| W8 | `app/scheduler.py`, `app/services/{alerts,digest}.py` (new), `db/repos/runs.py` (new), `scripts/*` |
| W9 | `app/services/*`, `static/js/*`, `resumes/coverletter.py`, optional `tools/oj-autofill.user.js` (new) |
| W10 | `tests/*`, `tools/seed_fixtures.py` (new) |

## Appendix B — API surface (current, for refactor reference)

`GET /health` · `GET /` · `GET /api/jobs` · `GET /api/jobs/export` · `GET /api/jobs/{pk}` · `PATCH /api/jobs/{pk}/status|notes|follow-up` · `GET /api/stats` · `GET|POST /api/schedule` · `GET /api/resume` · `GET /api/resume/profiles` · `PUT /api/resume` · `GET /api/resume/ats` · `POST /api/resume/tailor` · `GET /api/resume/export` · `GET /api/resume/yamlcv-status` · `GET /api/resume/built[/{name}]` · `POST /api/resume/build` · `GET /api/events` · `GET /api/skills` · `GET /api/skills/categories` · `POST /api/skills/refresh` · `POST /api/pipeline/run|check|stop` · `POST /api/keywords/apply` · `GET /api/keywords` · `GET|POST /api/scrape-scope` · `POST /api/jobs/reset`

**Missing and needed:** `GET /api/runs`, `GET /api/jobs/trash`, `POST /api/jobs/{pk}/restore`, `DELETE /api/jobs/{pk}`, `GET /api/jobs/duplicates`, `POST /api/jobs/merge`, `GET|POST /api/alert-rules`, `GET /api/config`, `POST /api/config/reload`, `GET /api/analytics/*`.

## Appendix C — Config keys

| Key | Today | After |
|---|---|---|
| `base_url`, `api_url` | used | unchanged |
| `db_path` | used | unchanged |
| `request_delay` | used | + jitter, + `max_requests_per_run` |
| `max_retries` | used | unchanged |
| `enrich_workers` | manual only (**auto-run ignores it**) | honored everywhere |
| `enrich_interval_days` | **ignored** (scheduler hard-codes 7) | honored + rename to `enrich_interval_days` in UI |
| `user_agent` | used | unchanged |
| `backup_retention_days` | used | unchanged |
| `llm_base_url` / `llm_api_key` / `llm_model` | used, restart to change | + live reload, + `/api/config` redaction |
| `resume_sources` | used, **hard-coded personal default** | default `[resumes/]` only |
| *(new)* `fx_to_php.usd` | — | salary normalization |
| *(new)* `digest_hour`, `quiet_hours` | — | W8.2/W8.4 |
| *(new)* `feature_flags.*` | — | W9 |

## Appendix D — Test matrix

| Layer | Today | Target |
|---|---|---|
| Parsers | 294 lines, inline HTML strings | fixture-corpus driven + selector drift check |
| DB/repos | good | + migration-step tests, + perf budgets |
| API | happy paths | + error envelopes, + pagination contract, + concurrency |
| Pipeline | generator unit tests | + resumability, + politeness caps |
| Resume/ATS | 358 + 118 + 130 lines | + golden files, + hallucination-guard tests |
| Frontend | **none** | Playwright smoke + a11y scan |
| E2E | **none** | nightly fixture-backed journey |
| CI | **none** | lint + unit + e2e + perf gate on every PR |

---

## 9. What "done" looks like

A stranger clones the repo, runs two commands, and gets a working dashboard. It scrapes politely at 1 req/s, survives a site markup change loudly instead of silently, stores salaries that can actually be compared, finds any job in under 80 ms, works on a phone and by keyboard, tells the owner what to do next instead of just listing rows, never loses data on a destructive action, never lies about a fact in a resume, and records every automated run so that "why did nothing happen last night?" has an answer. Everything else in §8 stays unbuilt.
