"""
db/repos/tailored.py — the tailored document, stored once per (job, profile).

B9 (audit): the tailor endpoint returned a document and threw it away, then
/export re-ran the LLM at temperature 0.2 and handed back a different one. The
score you were shown and the file you downloaded were two different resumes.
"""

import json

import sqlite3


def put(conn: sqlite3.Connection, job_pk: int, profile: str, doc: dict,
        jobs_version: int = 0) -> None:
    conn.execute(
        "INSERT INTO tailored_resumes (job_id, profile, jobs_version, doc, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT (job_id, profile) DO UPDATE SET "
        "doc = excluded.doc, jobs_version = excluded.jobs_version, "
        "created_at = excluded.created_at",
        (job_pk, profile, jobs_version, json.dumps(doc, ensure_ascii=False)),
    )
    conn.commit()


def get(conn: sqlite3.Connection, job_pk: int, profile: str) -> dict | None:
    """The stored tailored document, or None if this pair was never tailored."""
    row = conn.execute(
        "SELECT doc FROM tailored_resumes WHERE job_id = ? AND profile = ?",
        (job_pk, profile),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["doc"])
    except ValueError:
        return None


def clear(conn: sqlite3.Connection, job_pk: int) -> int:
    """Drop every stored tailoring for one job (used when the job's content changes)."""
    return conn.execute("DELETE FROM tailored_resumes WHERE job_id = ?",
                        (job_pk,)).rowcount