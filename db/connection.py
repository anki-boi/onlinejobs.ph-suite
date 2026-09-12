"""
db/connection.py -- SQLite connection management.

Creates the database with the full schema on first run.
Migrations run exactly once per DB, gated by PRAGMA user_version --
repeated init_db() calls (one per request) are cheap no-ops.
Per-request connections (not a shared global) to avoid cross-thread issues.

Set JOBS_DB_PATH (abs or relative to project root) to use an alternate DB file
(e.g. a sandbox copy for testing).
"""

import os
import sqlite3
from pathlib import Path

import db.migrate as dbmigrate  # versioned migration registry (W2.7)

# Bump when the migration in db/migrate.py changes. A DB file at a lower
# version runs each unapplied step exactly once, on the next init_db(); a
# fresh file runs all steps as no-ops and lands at this version.
SCHEMA_VERSION = 7

BASE_DIR = Path(__file__).resolve().parent.parent

TABLES = """
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          INTEGER UNIQUE,
            job_url         TEXT    UNIQUE NOT NULL,
            title           TEXT,
            company         TEXT,
            description     TEXT,
            salary          TEXT,
            location        TEXT,
            hours_per_week  TEXT,
            work_type       TEXT,
            posted_date     TEXT,
            date_updated    TEXT,
            skills          TEXT,
            employer_id     INTEGER,
            search_keyword  TEXT,
            search_category TEXT,
            scrape_status   TEXT    DEFAULT '',
            scrape_reason   TEXT    DEFAULT '',
            status          TEXT    DEFAULT 'New',
            filter_hidden   INTEGER NOT NULL DEFAULT 0,
            pre_filter_status TEXT  DEFAULT '',
            date_applied    TEXT    DEFAULT '',
            notes           TEXT    DEFAULT '',
            follow_up       TEXT    DEFAULT '',
            date_found      TEXT,
            last_checked    TEXT,
            repost_of       INTEGER REFERENCES jobs(id),
            salary_min      REAL,
            salary_max      REAL,
            salary_currency TEXT,
            salary_monthly_min REAL,
            salary_monthly_max REAL,
            norm_title      TEXT,
            deleted_at      TEXT,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS job_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            old_status  TEXT,
            new_status  TEXT,
            changed_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ats_scores (
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            profile     TEXT    NOT NULL,
            total       REAL    NOT NULL,
            updated_at  TEXT    NOT NULL,
            PRIMARY KEY (job_id, profile)
        );

        CREATE TABLE IF NOT EXISTS skill_tags (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            parent_id     INTEGER,
            slug          TEXT,
            category_path TEXT
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """

# W2.7: indexes are created only *after* migrations — a legacy jobs table may
# not have job_id yet when init_db() first sees it. Each def lists the table
# and columns it needs; _create_indexes() only creates the ones whose columns
# exist (fresh DBs via SCHEMA have them all).
_INDEX_DEFS = [
    ("idx_jobs_status", "jobs", ("status",)),
    ("idx_jobs_job_id", "jobs", ("job_id",)),
    ("idx_jobs_scrape_status", "jobs", ("scrape_status",)),
    ("idx_jobs_last_checked", "jobs", ("last_checked",)),
    ("idx_jobs_salary_monthly", "jobs", ("salary_monthly_min",)),
    ("idx_jobs_norm_title_employer", "jobs", ("norm_title", "employer_id")),
    ("idx_jobs_deleted_at", "jobs", ("deleted_at",)),
    ("idx_jobs_date_found", "jobs", ("date_found", "id")),
    ("idx_jobs_status_datefound", "jobs", ("status", "date_found", "id")),
    ("idx_jobs_scrape_status_datefound", "jobs", ("scrape_status", "date_found", "id")),
    ("idx_jobs_salary_monthly_max", "jobs", ("salary_monthly_max",)),
    ("idx_history_job_id", "job_history", ("job_id",)),
]

INDEXES = "\n".join(
    f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(cols)});"
    for name, table, cols in _INDEX_DEFS
) + "\n"

SCHEMA = TABLES + "\n" + INDEXES


def load_config() -> dict:
    """The live merged config (W1.4): app/config.py owns the state — reload()
    replaces it, so this no longer caches a stale copy forever."""
    from app import config as appconfig
    return appconfig.get()


def get_config() -> dict:
    """Public accessor for the merged config (lazy-loaded)."""
    return load_config()


_env_db = os.environ.get("JOBS_DB_PATH")

# Mutable module-level for direct access and test monkeypatching.
DB_PATH: str | Path | None = None  # set by tests; resolved lazily otherwise


def _resolve_db_path() -> Path:
    """Resolve the actual DB path (lazy so config doesn't race with init)."""
    if DB_PATH is not None:
        return Path(DB_PATH)
    path_str = _env_db or load_config().get("db_path", "jobs.db")
    if _env_db and os.path.isabs(_env_db):
        return Path(_env_db)
    return BASE_DIR / path_str


def get_db_path(db_path: str | Path | None = None) -> Path:
    """Public accessor for the resolved DB path.

    An explicit `db_path` (if given) wins over env/config (W2.1). Relative
    paths resolve against the project dir, matching `_resolve_db_path`.
    """
    if db_path is not None:
        candidate = Path(db_path)
        return candidate if candidate.is_absolute() else BASE_DIR / candidate
    return _resolve_db_path()


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a new SQLite connection with row factory.

    `timeout=30` sets the busy-wait: a locked DB (e.g. a pipeline writing)
    makes callers wait up to 30s instead of failing instantly with
    `database is locked`.
    """
    path = str(db_path or _resolve_db_path())
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Create tables + run migrations. Returns the connection.

    Migrations are gated on PRAGMA user_version: they run at most once per
    database file. This matters because every request path touches init_db()
    -- an ungated migration would re-run its UPDATE statements (write locks,
    and stale data overwrites) on every single request.
    """
    if conn is None:
        conn = get_conn()
    _create_tables(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        # W2.7: versioned steps — each unapplied version is applied in order
        # and user_version bumps after each, so a failing step retries next
        # boot instead of re-running (or skipping) the whole batch.
        dbmigrate.run(conn, SCHEMA_VERSION)
    _create_indexes(conn)  # after migrations: legacy tables may lack indexed cols
    conn.commit()
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(TABLES)


def _create_indexes(conn: sqlite3.Connection) -> None:
    # Only create indexes whose columns/tables exist yet — a legacy DB may be
    # here before the migration that adds them has run (W2.7).
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    history = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_history'"
    ).fetchone()
    stmts = []
    for name, table, cols in _INDEX_DEFS:
        have = job_cols if table == "jobs" else ({"job_id"} if history else set())
        if all(c in have for c in cols):
            stmts.append(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(cols)});")
    if stmts:
        conn.executescript("\n".join(stmts))
