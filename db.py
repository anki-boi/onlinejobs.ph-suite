"""
db.py — Single source of truth for all database access.
"""

import sqlite3
from pathlib import Path

DB_FILE = "jobs.db"

STATUSES = ["New", "Interested", "Applied", "Interviewing", "Offer", "Hired", "Rejected", "Hidden"]

# ── Connection ────────────────────────────────────────────────────────────────

def get_conn(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_link      TEXT    UNIQUE NOT NULL,
            job_title     TEXT,
            company       TEXT,
            description   TEXT,
            salary        TEXT,
            search_tag    TEXT,
            tags_found    TEXT,
            date_found    TEXT,
            status        TEXT    DEFAULT 'New',
            date_applied  TEXT    DEFAULT '',
            notes         TEXT    DEFAULT '',
            follow_up     TEXT    DEFAULT '',
            created_at    TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_tags (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Non-destructive migrations for older DBs."""
    # Rename tag → search_tag
    try:
        conn.execute("ALTER TABLE jobs RENAME COLUMN tag TO search_tag")
    except sqlite3.OperationalError:
        pass
    # Add any missing columns
    for col_def in (
        "search_tag  TEXT DEFAULT ''",
        "tags_found  TEXT DEFAULT ''",
        "notes       TEXT DEFAULT ''",
        "follow_up   TEXT DEFAULT ''",
        "date_applied TEXT DEFAULT ''",
    ):
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    # Normalise blank/null status → 'New'
    conn.execute("UPDATE jobs SET status = 'New' WHERE status IS NULL OR status = ''")


# ── Jobs ──────────────────────────────────────────────────────────────────────

def get_jobs(
    conn: sqlite3.Connection,
    include_hidden: bool = False,
    status_filter: list[str] | None = None,
) -> list[sqlite3.Row]:
    clauses = []
    params: list = []

    if not include_hidden:
        clauses.append("status != 'Hidden'")

    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        clauses.append(f"status IN ({placeholders})")
        params.extend(status_filter)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY id DESC", params
    ).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def insert_link(conn: sqlite3.Connection, job_link: str, search_tag: str, date_found: str) -> bool:
    """Insert a new job stub. Returns True if inserted, False if duplicate."""
    try:
        conn.execute(
            "INSERT INTO jobs (job_link, search_tag, date_found, status) VALUES (?, ?, ?, 'New')",
            (job_link, search_tag, date_found),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Already exists — update search_tag in case found under new category
        conn.execute(
            "UPDATE jobs SET search_tag = ? WHERE job_link = ?",
            (search_tag, job_link),
        )
        conn.commit()
        return False


def update_job_details(conn: sqlite3.Connection, job_id: int, details: dict) -> None:
    """Update scraped detail fields. Only writes non-None values."""
    fields = {"status": details["status"]}
    for col in ("description", "job_title", "company", "salary", "tags_found"):
        val = details.get(col)
        if val is not None:
            fields[col] = val
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", list(fields.values()) + [job_id])
    conn.commit()


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status}")
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def update_job_notes(conn: sqlite3.Connection, job_id: int, notes: str) -> None:
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
    conn.commit()


def hide_jobs_by_keywords(conn: sqlite3.Connection, keywords: list[str]) -> int:
    """Set status=Hidden for all non-Hidden jobs matching any negative keyword."""
    if not keywords:
        return 0
    updated = 0
    rows = conn.execute(
        "SELECT id, job_title, description, company FROM jobs WHERE status != 'Hidden'"
    ).fetchall()
    for row in rows:
        haystack = " ".join([
            row["job_title"] or "",
            row["description"] or "",
            row["company"] or "",
        ]).lower()
        if any(kw.lower() in haystack for kw in keywords):
            conn.execute("UPDATE jobs SET status = 'Hidden' WHERE id = ?", (row["id"],))
            updated += 1
    conn.commit()
    return updated


def filter_by_positive_keywords(conn: sqlite3.Connection, keywords: list[str]) -> int:
    """
    Hide jobs that do NOT match any positive keyword.
    Only applies to jobs currently in 'New' status (not already actioned).
    Returns number hidden.
    """
    if not keywords:
        return 0
    updated = 0
    rows = conn.execute(
        "SELECT id, job_title, description, company FROM jobs WHERE status = 'New'"
    ).fetchall()
    for row in rows:
        haystack = " ".join([
            row["job_title"] or "",
            row["description"] or "",
            row["company"] or "",
        ]).lower()
        if not any(kw.lower() in haystack for kw in keywords):
            conn.execute("UPDATE jobs SET status = 'Hidden' WHERE id = ?", (row["id"],))
            updated += 1
    conn.commit()
    return updated


def get_existing_links(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT job_link FROM jobs").fetchall()}


def get_hidden_links(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT job_link FROM jobs WHERE status = 'Hidden'"
    ).fetchall()}


def get_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["count"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


# ── Tags ──────────────────────────────────────────────────────────────────────

def get_skill_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM skill_tags ORDER BY name").fetchall()


def upsert_skill_tags(conn: sqlite3.Connection, tags: list[dict]) -> int:
    conn.executemany(
        """INSERT INTO skill_tags (id, name, updated_at)
           VALUES (:id, :name, datetime('now'))
           ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at""",
        tags,
    )
    conn.commit()
    return len(tags)
