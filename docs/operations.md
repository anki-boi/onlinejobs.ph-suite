# Operations

Install, configuration, autostart, backup/restore, logs, and
troubleshooting for the Job Hunter Dashboard.

## Install

**Windows:** double-click `install.bat` (creates a venv and installs
`requirements.txt`), then `run.bat` to start on http://127.0.0.1:8371.

**Any platform:**

```bash
pip install -r requirements.txt
python main.py            # --port, --host, --skip-skills available
git config core.hooksPath .githooks   # once per clone: gate runs on every push
```

Fresh clone, first run: open the dashboard, fill in the resume
(name, email, phone, skills), then Scrape — or wait for the auto-run.

## Configuration

`config.json` (checked in) is overlaid by `config.local.json`
(gitignored — put secrets and personal paths here). Copy
`config.local.json.example` to start. Every key is documented in the
README's configuration table.

Edited files take effect after **Reload config** (the button in the config
note banner, or `POST /api/config/reload`) — no restart needed (W1.4). One
caveat: the HTTP client is built once, so scraper keys (`request_delay`,
`max_retries`, `user_agent`, URLs) apply on the next app start; LLM and
auto-run keys are live. LLM keys: `llm_base_url` / `llm_api_key` / `llm_model`
point at any OpenAI-compatible endpoint (OpenAI, a local vLLM box, …).
Without them, tailor and 1-page-CV return 503; everything else works.

The active database file is `config.json:db_path` (relative → project
dir). The environment variable `JOBS_DB_PATH` (absolute, or relative to
the project dir) overrides it — used to run against a copy:

```bash
JOBS_DB_PATH=/tmp/sandbox.db python main.py --port 8372
```

## Autostart (Windows)

- `scripts/make_autostart.bat` — creates the `JobHunter` scheduled
  task: runs `pythonw main.py` at logon (no console window).
- `scripts/disable_autostart.bat` — disables that task.

To schedule the daily backup yourself:

```bat
schtasks /create /tn "JobHunter-Backup" /sc daily /st 03:00 ^
  /tr "python C:\path\to\onlinejobs.ph-suite\scripts\backup.py"
```

(The owner's machine runs exactly this.)

## Backup & restore

`scripts/backup.py` snapshots the database with `VACUUM INTO` into
`backups/` and prunes to the newest N copies (N =
`backup_retention_days`, default 7). Direct usage:

```bash
python scripts/backup.py [db_path] [backup_dir] [keep]
```

- **Backup:** run it on a schedule (see above) or by hand; a running app
  does not affect the snapshot (WAL + `VACUUM INTO` reads a consistent
  state). If it ever fails with "database is locked", it raced a write —
  just re-run it.
- **Restore:** stop the app, copy the chosen snapshot from `backups/`
  over `jobs.db` (and remove any `jobs.db-wal` / `jobs.db-shm`), start
  the app. Migrations run on boot only if the snapshot's
  `user_version` is older than the code's — a snapshot from an older
  version migrates forward automatically (W2.7).

## Logs

`job_hunter.log*` in the project dir: rotating files (5 MB each, 3
backups). When running interactively, a copy of INFO+ also streams to
stderr. Run errors land in `app_settings.last_error` and show in the
dashboard's auto-run panel.

## Troubleshooting

| Symptom | What it means / do |
|---|---|
| `GET /health` → 503 `site: unreachable` | The site is down or your network can't reach it; scraping won't work until it does. The app still serves stored data. |
| `GET /health` → 200 `db: locked` | Another process holds the DB (a second app instance, or a stuck backup); it should clear within seconds. If it persists, stop other processes touching `jobs.db`. |
| Two instances "stuck" | The pipeline lock row shows the holder PID (settings table / auto-run panel). A dead holder's PID is detected and the lock auto-recovers on next run (W2.6). |
| `structure change` alert | The search page stopped yielding job boxes while the site claims results — the site layout likely changed; check `scraper/parsers.py` against a saved page. |
| `429`s during a run | Expected under heavy use — the client backs off exponentially honoring `Retry-After`; when retries exhaust on one page, that page is reported as an error and the run continues. Only a hard error (e.g. the DB) records the run as `failed`. |
| Tailor/CV returns 503 | No LLM configured in `config.local.json` (see Configuration). |
| Port already in use | `--port 8372`, or stop the other instance (`disable_autostart.bat` + Task Manager if it's the logon one). |
