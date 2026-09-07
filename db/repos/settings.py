"""
db/repos/settings.py — tiny key/value store for app state (auto-run config,
last/next run timestamps, last error).
"""

import sqlite3
from pathlib import Path

import db.connection as dbconn

BACKUP_RETENTION_KEY = "backup_retention_days"
DEFAULT_BACKUP_RETENTION_DAYS = 7


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    """Get an integer setting (falls back to `default`)."""
    raw = get(conn, key)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def set_int(conn: sqlite3.Connection, key: str, value: int) -> None:
    """Set an integer setting."""
    set(conn, key, str(value))


def set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
