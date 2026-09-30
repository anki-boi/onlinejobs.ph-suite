"""
db/repos/runs.py — the run history (audit P8).

Written once per pipeline run by scheduler.record_run_status, read by
GET /api/runs and the "Recent runs" panel.
"""

import json
import sqlite3


def record(conn: sqlite3.Connection, kind: str, status: str, *,
           scope: dict | None = None, summary: dict | None = None,
           error: str | None = None) -> None:
    s = summary or {}
    # The pipeline's tally calls the harvest count "new"; the table calls it
    # "inserted". Accept either rather than silently recording zero.
    inserted = s.get("inserted") if s.get("inserted") is not None else s.get("new", 0)
    conn.execute(
        "INSERT INTO runs (finished, kind, scope, inserted, enriched, closed, "
        "errors, status, error) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, json.dumps(scope) if scope else None,
         int(inserted or 0), int(s.get("enriched", 0) or 0),
         int(s.get("closed", 0) or 0), int(s.get("errors", 0) or 0),
         status, error),
    )
    conn.commit()


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
    ).fetchall()