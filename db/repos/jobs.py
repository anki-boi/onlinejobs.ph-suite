"""
db/repos/jobs.py — All job CRUD and query operations.
"""

import sqlite3
from datetime import datetime

STATUSES = [
    "New", "Interested", "Applied", "Interviewing",
    "Offer", "Hired", "Rejected", "Hidden",
]

SCRAPE_STATUSES = ["Open", "Closed", ""]


# Columns the API may sort by (Excel-style header sorting).
SORTABLE = {
    "title", "company", "salary", "location", "hours_per_week", "work_type",
    "posted_date", "date_updated", "date_found", "status", "scrape_status", "skills",
}

def _split_multi(value: str | None) -> list[str]:
    """'a,b, c' → ['a','b','c'] (stripped, non-empty)."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def get_jobs(
    conn: sqlite3.Connection,
    page: int = 1,
    per_page: int = 50,
    status: str | None = None,
    search: str | None = None,
    include_hidden: bool = False,
    work_type: str | None = None,
    skill: str | None = None,
    skills: str | None = None,
    scrape_status: str | None = None,
    sort: str | None = None,
    order: str = "desc",
    title: str | None = None,
    company: str | None = None,
    salary: str | None = None,
    location: str | None = None,
    hours: str | None = None,
    posted: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Return (rows, total_count) with optional filters and pagination.

    Multi-value filters (status, work_type, scrape_status, skills) take
    comma-separated strings and OR within their own group.
    Text filters (title, company, salary, location, hours) are LIKE matches.
    `sort` must be in SORTABLE, else the default (newest first) applies.
    """
    clauses: list[str] = []
    params: list = []

    if not include_hidden:
        clauses.append("status != 'Hidden'")

    if status:
        statuses = _split_multi(status)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

    if work_type:
        wts = _split_multi(work_type)
        if wts:
            placeholders = ",".join("?" * len(wts))
            clauses.append(f"work_type IN ({placeholders})")
            params.extend(wts)

    if scrape_status:
        ss = _split_multi(scrape_status)
        if ss:
            placeholders = ",".join("?" * len(ss))
            clauses.append(f"scrape_status IN ({placeholders})")
            params.extend(ss)

    if skills:
        sks = _split_multi(skills)
        if sks:
            clauses.append("(" + " OR ".join("skills LIKE ?" for _ in sks) + ")")
            params.extend(f"%{s}%" for s in sks)

    if skill:
        clauses.append("skills LIKE ?")
        params.append(f"%{skill}%")

    for col, val in (("title", title), ("company", company), ("salary", salary),
                     ("location", location), ("hours_per_week", hours), ("posted_date", posted)):
        if val:
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{val}%")

    if search:
        like = f"%{search}%"
        clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ? OR search_keyword LIKE ?)")
        params.extend([like, like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM jobs {where}", params
    ).fetchone()[0]

    if sort in SORTABLE:
        order_sql = "ASC" if order.strip().lower() == "asc" else "DESC"
        order_by = f"{sort} {order_sql}"
    else:
        order_by = "date_found DESC, id DESC"

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT * FROM jobs {where} ORDER BY {order_by}, id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return rows, total


def get_job(conn: sqlite3.Connection, job_pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_pk,)).fetchone()


def get_job_by_ojid(conn: sqlite3.Connection, oj_job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (oj_job_id,)).fetchone()


def upsert_stub(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    job_url: str,
    title: str | None = None,
    work_type: str | None = None,
    company: str | None = None,
    posted_date: str | None = None,
    salary: str | None = None,
    location: str | None = None,
    hours: str | None = None,
    skills: list[str] | None = None,
    search_keyword: str | None = None,
    search_category: str | None = None,
) -> tuple[int, bool]:
    """
    Insert a new job stub from the search list view.
    Returns (row_id, is_new). If the job already exists (by job_id or job_url),
    update the list-view fields and return the existing ID.
    """
    skills_str = ", ".join(skills) if skills else None
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check by job_id first, then job_url
    existing = None
    if job_id:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if not existing:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE job_url = ?", (job_url,)
        ).fetchone()

    if existing:
        row_id = existing["id"]
        # Only update fields that are not None (don't clobber existing data)
        updates = []
        vals = []
        for col, val in [
            ("title", title), ("work_type", work_type), ("company", company),
            ("posted_date", posted_date), ("salary", salary), ("location", location),
            ("hours_per_week", hours),
        ]:
            if val is not None:
                updates.append(f"{col} = ?")
                vals.append(val)
        if skills_str:
            updates.append("skills = ?")
            vals.append(skills_str)
        if search_keyword:
            updates.append("search_keyword = ?")
            vals.append(search_keyword)
        if search_category:
            updates.append("search_category = ?")
            vals.append(search_category)
        if updates:
            vals.append(row_id)
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", vals)
        conn.commit()
        return row_id, False

    conn.execute(
        """INSERT INTO jobs
           (job_id, job_url, title, work_type, company, posted_date,
            salary, location, hours_per_week, skills, search_keyword,
            search_category, date_found, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'New')""",
        (
            job_id, job_url, title, work_type, company, posted_date,
            salary, location, hours, skills_str, search_keyword,
            search_category, date_now,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"], True


def enrich_job(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    title: str | None = None,
    company: str | None = None,
    description: str | None = None,
    salary: str | None = None,
    hours_per_week: str | None = None,
    work_type: str | None = None,
    date_updated: str | None = None,
    skills: list[str] | None = None,
    employer_id: int | None = None,
    is_closed: bool = False,
    close_reason: str | None = None,
) -> None:
    """Update a job with detail-page data. Only overwrites non-None values."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scrape_status = "Closed" if is_closed else "Open"

    fields = {
        "scrape_status": scrape_status,
        "scrape_reason": close_reason or "",
        "last_checked": now,
    }
    for col, val in [
        ("title", title), ("company", company), ("description", description),
        ("salary", salary), ("hours_per_week", hours_per_week),
        ("work_type", work_type), ("date_updated", date_updated),
        ("employer_id", employer_id),
    ]:
        if val is not None:
            fields[col] = val
    if skills is not None:
        fields["skills"] = ", ".join(skills)

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [row_id]
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", vals)
    conn.commit()


def update_status(conn: sqlite3.Connection, row_id: int, new_status: str) -> None:
    """Update workflow status + write to history.

    A manual status change always takes over from the keyword filter:
    the filter_hidden flag is cleared so the filter will never clobber
    a status the user explicitly set.
    """
    if new_status not in STATUSES:
        raise ValueError(f"Invalid status: {new_status}")
    row = get_job(conn, row_id)
    if not row:
        raise ValueError(f"Job {row_id} not found")
    old_status = row["status"]
    conn.execute(
        "UPDATE jobs SET status = ?, filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
        (new_status, row_id),
    )
    conn.execute(
        "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, ?, ?)",
        (row_id, old_status, new_status),
    )
    conn.commit()


def update_notes(conn: sqlite3.Connection, row_id: int, notes: str) -> None:
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, row_id))
    conn.commit()


def update_follow_up(conn: sqlite3.Connection, row_id: int, follow_up: str) -> None:
    conn.execute("UPDATE jobs SET follow_up = ? WHERE id = ?", (follow_up, row_id))
    conn.commit()


def get_existing_job_ids(conn: sqlite3.Connection) -> set[int]:
    """Return set of all known job_ids (for dedup during harvest)."""
    rows = conn.execute("SELECT job_id FROM jobs WHERE job_id IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def get_existing_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT job_url FROM jobs").fetchall()
    return {r[0] for r in rows}


def get_jobs_needing_enrichment(
    conn: sqlite3.Connection,
    max_age_days: int = 7,
    status_filter: list[str] | None = None,
) -> list[sqlite3.Row]:
    """Jobs that need detail-page re-check: never checked, or checked > max_age_days ago."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clauses = [
        "(last_checked IS NULL OR last_checked = '' OR last_checked < datetime('now', ?))"
    ]
    params: list = [f"-{max_age_days} days"]

    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        clauses.append(f"status IN ({placeholders})")
        params.extend(status_filter)

    where = f"WHERE {' AND '.join(clauses)}"
    return conn.execute(
        f"SELECT id, job_url, status FROM jobs {where} ORDER BY id", params
    ).fetchall()


def get_stats(conn: sqlite3.Connection) -> dict:
    """Status counts + scrape_status counts + totals."""
    status_rows = conn.execute(
        "SELECT status, COUNT(*) as c FROM jobs GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["c"] for r in status_rows}
    stats["total"] = sum(stats.values())

    scrape_rows = conn.execute(
        "SELECT scrape_status, COUNT(*) as c FROM jobs WHERE scrape_status != '' GROUP BY scrape_status"
    ).fetchall()
    stats["scrape"] = {r["scrape_status"]: r["c"] for r in scrape_rows}

    stats["filter_hidden"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE filter_hidden = 1"
    ).fetchone()[0]

    return stats


def get_job_history(conn: sqlite3.Connection, job_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM job_history WHERE job_id = ? ORDER BY id DESC",
        (job_pk,),
    ).fetchall()
