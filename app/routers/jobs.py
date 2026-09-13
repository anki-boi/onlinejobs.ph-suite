"""app/routers/jobs.py — job listing/detail/mutation routes + CSV export +
stats + full reset. (W5.1 split from server.py; no behaviour change.)"""

import csv as _csv
import hashlib
import io as _io
import sqlite3

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRouter
from app import events as events_hub
from app.schemas import FollowUpUpdate, NotesUpdate, StatusUpdate

from app import server as srv
from db.repos import jobs as job_repo

router = APIRouter()

_EXPORT_COLS = ["id", "job_id", "title", "company", "description", "salary",
                "location", "hours_per_week", "work_type", "posted_date",
                "date_updated", "skills", "status", "notes", "follow_up"]


@router.get("/api/jobs/export")
def export_jobs():
    """Stream the full job set as CSV. No pagination — one complete dump."""
    conn = srv.get_db()
    rows = conn.execute(f"SELECT {','.join(_EXPORT_COLS)} FROM jobs ORDER BY id").fetchall()

    output = _io.StringIO()
    writer = _csv.DictWriter(output, fieldnames=_EXPORT_COLS)
    writer.writeheader()
    for r in rows:
        writer.writerow(dict(r))

    content = output.getvalue().encode("utf-8")

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="jobs_export.csv"'},
    )


@router.get("/api/jobs")
def list_jobs(request: Request,
              page: int = 1,
    per_page: int = 50,
    status: str | None = None,
    search: str | None = None,
    include_hidden: bool = False,
    work_type: str | None = None,
    skill: str | None = None,
    skills: str | None = None,  # comma-separated OR filter
    scrape_status: str | None = None,  # comma-separated, e.g. "Open,Closed"
    sort: str | None = None,  # any sortable column, else newest-first
    order: str = "desc",  # asc | desc
    title: str | None = None,
    company: str | None = None,
    salary: str | None = None,
    location: str | None = None,
    hours: str | None = None,
    posted_from: str | None = None,  # inclusive range start (YYYY-MM-DD) on the displayed posted date
    posted_to: str | None = None,    # inclusive range end
    has_salary: bool = False,
    salary_min_monthly: float | None = None,   # job's max (PHP/month) >= this
    salary_max_monthly: float | None = None,   # job's min (PHP/month) <= this
    salary_currency: str | None = None,        # comma-separated, e.g. "USD,PHP"
    min_ats: int = 0,  # hide jobs whose best-profile ATS score is below this
):
    # W5.3: per_page is capped — one page must stay small enough to render.
    if per_page > 500:
        raise HTTPException(400, "per_page is limited to 500 (use /api/jobs/export for full dumps)")
    if per_page < 1:
        per_page = 1
    page = max(1, page)
    conn = srv.get_db()
    from app.services import ats_cache
    ats_cache.ensure_fresh(
        conn, srv._ats_cache_key(conn), srv._masters(),
        conn.execute("SELECT * FROM jobs"), srv._job_dict_for_resume)
    rows, total = job_repo.get_jobs(
        conn, page=page, per_page=per_page, status=status,
        search=search, include_hidden=include_hidden,
        work_type=work_type, skill=skill, skills=skills,
        scrape_status=scrape_status, sort=sort, order=order,
        title=title, company=company, salary=salary,
        location=location, hours=hours,
        posted_from=posted_from, posted_to=posted_to,
        has_salary=has_salary,
        min_ats=min_ats,
        salary_min_monthly=salary_min_monthly,
        salary_max_monthly=salary_max_monthly,
        salary_currency=salary_currency,
    )
    items = [dict(r) for r in rows]
    payload = {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        # W5.3: opaque cursor for the next page (null on the last one).
        "next_cursor": str(page + 1) if page * per_page < total else None,
    }
    # W5.4: ETag from the jobs table version + the exact query string.
    # Repeat GETs with If-None-Match get a 304 (no body) until the data
    # or the query changes.
    version = job_repo.get_jobs_version(conn)
    etag = '"%s"' % hashlib.sha1(f"{version}:{request.url.query}".encode()).hexdigest()[:16]
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    out = JSONResponse(payload)
    out.headers["ETag"] = etag
    return out


@router.get("/api/jobs/{job_pk}")
def get_job(job_pk: int):
    conn = srv.get_db()
    row = job_repo.get_job(conn, job_pk)
    if not row:
        raise HTTPException(404, "Job not found")
    result = dict(row)
    result["history"] = [dict(h) for h in job_repo.get_job_history(conn, job_pk)]
    return result


@router.patch("/api/jobs/{job_pk}/status")
def update_status(job_pk: int, body: StatusUpdate):
    conn = srv.get_db()
    if not job_repo.get_job(conn, job_pk):
        raise HTTPException(404, "Job not found")
    try:
        job_repo.update_status(conn, job_pk, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.patch("/api/jobs/{job_pk}/notes")
def update_notes(job_pk: int, body: NotesUpdate):
    conn = srv.get_db()
    job_repo.update_notes(conn, job_pk, body.notes)
    return {"ok": True}


@router.patch("/api/jobs/{job_pk}/follow-up")
def update_follow_up(job_pk: int, body: FollowUpUpdate):
    conn = srv.get_db()
    job_repo.update_follow_up(conn, job_pk, body.follow_up)
    return {"ok": True}


@router.get("/api/stats")
def stats():
    return job_repo.get_stats(srv.get_db())


@router.post("/api/jobs/reset")
def reset_jobs():
    """Delete every job + status history. Keeps: saved keyword rules, scrape
    scope, scheduler settings, resume masters, backups."""
    conn = srv.get_db()
    try:
        conn.execute("DELETE FROM job_history")  # also FK-cascades from jobs
        n = conn.execute("DELETE FROM jobs").rowcount
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise HTTPException(
            503, f"Database busy ({exc}) — the pipeline may be writing; try again in a moment"
        )
    events_hub.publish("jobs_reset", {"deleted_jobs": n})
    return {"deleted_jobs": n}
