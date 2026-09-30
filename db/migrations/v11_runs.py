"""
V11: a `runs` table — the answer to "what happened last night?" (audit P8).

One last_run/last_status triple was the entire history, so a run that inserted
nothing and a run that errored out at 3am looked identical. Each pipeline run now
writes a row: kind, scope, counts, status.
"""

import logging

log = logging.getLogger(__name__)


def step(conn):
    """v11: runs history table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            started   TEXT    NOT NULL DEFAULT (datetime('now')),
            finished  TEXT,
            kind      TEXT    NOT NULL,           -- harvest | check | auto
            scope     TEXT,                       -- JSON: keyword/categories/skills
            inserted  INTEGER NOT NULL DEFAULT 0,
            enriched  INTEGER NOT NULL DEFAULT 0,
            closed    INTEGER NOT NULL DEFAULT 0,
            errors    INTEGER NOT NULL DEFAULT 0,
            status    TEXT    NOT NULL,           -- completed | stopped | failed
            error     TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started DESC)")
    log.info("v11: runs history table ready")
    conn.commit()