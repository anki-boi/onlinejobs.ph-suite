"""
db/migrate.py — Non-destructive migrations for older database files.

Runs exactly once per DB file (gated by PRAGMA user_version in
connection.init_db) and bumps the version when done.

Handles the transition from the original schema (job_link, job_title, search_tag,
tags_found) to the new schema (job_url, title, search_keyword, skills, etc.),
seeds legacy rows, and repairs scrape_status values that the pre-2.0 code
stamped onto jobs that were never actually checked.
"""

import logging

log = logging.getLogger(__name__)

# Old column → new column mappings
_COLUMN_RENAMES = [
    ("job_link", "job_url"),
    ("job_title", "title"),
    ("search_tag", "search_keyword"),
    ("tags_found", "skills"),
]


def migrate(conn) -> None:
    """Detect old schema and migrate data to new columns."""
    # v3: key/value table for auto-run state (new DBs already have it via SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")

    existing = _existing_columns(conn, "jobs")

    # Add any missing new columns
    new_cols = {
        "job_id":          "INTEGER",
        "job_url":         "TEXT",
        "title":           "TEXT",
        "location":        "TEXT",
        "hours_per_week":  "TEXT",
        "work_type":       "TEXT",
        "posted_date":     "TEXT",
        "date_updated":    "TEXT",
        "skills":          "TEXT",
        "employer_id":     "INTEGER",
        "search_keyword":  "TEXT",
        "search_category": "TEXT",
        "scrape_status":   "TEXT DEFAULT ''",
        "scrape_reason":   "TEXT DEFAULT ''",
        "last_checked":    "TEXT",
        "filter_hidden":   "INTEGER NOT NULL DEFAULT 0",
        "pre_filter_status": "TEXT DEFAULT ''",
    }
    for col, col_type in new_cols.items():
        if col not in existing:
            log.info("Adding column jobs.%s", col)
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")

    # Copy data from old columns to new (if old columns exist and new are empty)
    for old_col, new_col in _COLUMN_RENAMES:
        if old_col in existing and new_col in existing:
            row = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {new_col} IS NULL OR {new_col} = ''"
            ).fetchone()
            if row[0] > 0:
                conn.execute(
                    f"UPDATE jobs SET {new_col} = {old_col} "
                    f"WHERE ({new_col} IS NULL OR {new_col} = '') AND {old_col} IS NOT NULL AND {old_col} != ''"
                )
                log.info("Migrated %s → %s", old_col, new_col)

    # Populate job_id from job_url (extract trailing number from slug)
    _populate_job_ids(conn)

    # Normalise old scrape-status values that were written into the workflow column
    # Old code wrote "Open"/"Closed" into status. Map them to sensible defaults.
    conn.execute("UPDATE jobs SET status = 'New'  WHERE status = 'Open'")
    conn.execute("UPDATE jobs SET status = 'Hidden' WHERE status = 'Closed'")
    conn.execute("UPDATE jobs SET status = 'New'  WHERE status IS NULL OR status = ''")

    # Seed scrape_status from the old status values where we can tell
    conn.execute("UPDATE jobs SET scrape_status = 'Open' WHERE scrape_status = '' AND status = 'New' AND job_url IS NOT NULL")

    # One-time repair (v2): the seed above (and the same un-gated UPDATE the
    # pre-2.0 code ran on every request) stamped 'Open' onto jobs that were
    # never actually checked. A job that has never been enriched has no
    # scrape status at all — its state is 'unknown', not 'Open'.
    conn.execute(
        "UPDATE jobs SET scrape_status = '', scrape_reason = '' "
        "WHERE (last_checked IS NULL OR last_checked = '') AND scrape_status IN ('Open', 'Closed')"
    )

    # Ensure unique constraint on job_url (add UNIQUE if missing by recreating)
    # SQLite doesn't support ADD CONSTRAINT, so we just rely on INSERT OR IGNORE
    # and the application-level dedup.


def _existing_columns(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _populate_job_ids(conn) -> None:
    """Extract numeric job ID from the URL slug for rows that don't have one yet."""
    import re
    rows = conn.execute(
        "SELECT id, job_url FROM jobs WHERE job_id IS NULL AND job_url IS NOT NULL"
    ).fetchall()
    for row in rows:
        url = row["job_url"]
        match = re.search(r"/job/[^/]+?-(\d+)$", url)
        if match:
            conn.execute(
                "UPDATE jobs SET job_id = ? WHERE id = ? AND job_id IS NULL",
                (int(match.group(1)), row["id"]),
            )
    if rows:
        log.info("Populated job_id for %d rows", len(rows))
