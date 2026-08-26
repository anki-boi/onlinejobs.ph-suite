"""
db/connection.py — SQLite connection management.

Creates the database with the full schema on first run.
Per-request connections (not a shared global) to avoid cross-thread issues.
"""

import json
import sqlite3
from pathlib import Path

from db.migrate import migrate

BASE_DIR = Path(__file__).resolve().parent.parent

SCHEMA = """
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
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS job_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            old_status  TEXT,
            new_status  TEXT,
            changed_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS skill_tags (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            parent_id     INTEGER,
            slug          TEXT,
            category_path TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_job_id       ON jobs(job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_scrape_status ON jobs(scrape_status);
        CREATE INDEX IF NOT EXISTS idx_jobs_last_checked ON jobs(last_checked);
        CREATE INDEX IF NOT EXISTS idx_history_job_id    ON job_history(job_id);
    """

def _load_config() -> dict:
    cfg_path = BASE_DIR / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text())
    return {}

_config = _load_config()
DB_PATH = BASE_DIR / _config.get("db_path", "jobs.db")


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a new SQLite connection with row factory.

    `timeout=30` sets the busy-wait: a locked DB (e.g. a pipeline writing)
    makes callers wait up to 30s instead of failing instantly with
    `database is locked`.
    """
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Create tables + run migrations. Returns the connection."""
    if conn is None:
        conn = get_conn()
    _create_tables(conn)
    migrate(conn)
    conn.commit()
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
