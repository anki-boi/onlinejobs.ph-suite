"""
db/repos/settings.py — tiny key/value store for app state (auto-run config,
last/next run timestamps, last error).
"""

import sqlite3


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
