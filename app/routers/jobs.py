"""app/routers/jobs.py — job listing/detail/mutation routes + CSV export +
stats + full reset. (W5.1 split from server.py; no behaviour change.)"""

import csv as _csv
import hashlib
import io as _io
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRouter
from app import events as events_hub
from app.schemas import FollowUpUpdate, NotesUpdate, StatusUpdate, JobQuery

from app import server as srv
from db.repos import jobs as job_repo

router = APIRouter(tags=['Jobs'])

_EXPORT_COLS = ["id", "job_id", "title", "company", "description", "salary",
                "salary_monthly_min", "salary_monthly_max", "salary_currency",
                "location", "hours_per_week", "work_type", "posted_date",
                "date_updated", "skills", "search_category", "status", "notes",
                "follow_up", "repost_of", "filter_hidden", "pre_filter_status",
                "scrape_status", "ats_fit", "ats_total", "ats_profile"]


def _filtered_jobs(q: JobQuery):
    """The one place that turns a JobQuery into rows — shared by the page and the
    CSV so the two can't drift (B11)."""
    conn = srv.get_db()
    from app.services import ats_cache
    ats_cache.ensure_fresh(
        conn, srv._ats_cache_key(conn), srv._masters(),
        conn.execute("SELECT * FROM jobs"), srv._job_dict_for_resume)
    return job_repo.get_jobs(
        conn, page=max(1, q.page), per_page=q.per_page, status=q.status,
        search=q.search, include_hidden=q.include_hidden,
        work_type=q.work_type, skill=q.skill, skills=q.skills,
        categories=q.categories, scrape_status=q.scrape_status,
        sort=q.sort, order=q.order, title=q.title, company=q.company,
        salary=q.salary, location=q.location, hours=q.hours,
        posted_from=q.posted_from, posted_to=q.posted_to,
        has_salary=q.has_salary, min_ats=q.min_ats, min_fit=q.min_fit,
        salary_min_monthly=q.salary_min_monthly,
        salary_max_monthly=q.salary_max_monthly,
        salary_currency=q.salary_currency, hide_reposts=q.hide_reposts,
        include_deleted=q.include_deleted,
    )


@router.get("/api/jobs/export")
def export_jobs(q: JobQuery = Depends()):

    """CSV of the jobs matching the current view — every /api/jobs filter applies, same names, same meaning (B11)."""

    rows, _total = _filtered_jobs(q.model_copy(update={"per_page": 0, "page": 1}))
    output = _io.StringIO()
    writer = _csv.DictWriter(output, fieldnames=_EXPORT_COLS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r[k] for k in _EXPORT_COLS})

    return Response(
        content=output.getvalue().encode("utf-8-sig"),   # BOM: Excel needs it for UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="jobs_export.csv"'},
    )


@router.get("/api/jobs")
def list_jobs(request: Request, q: JobQuery = Depends()):

    """List saved jobs with filters. Paginated: {items, page, per_page, total, next_cursor}; per_page max 500 (bigger is 400); repeat GETs with If-None-Match get 304 until data or query changes."""

    # W5.3: per_page is capped — one page must stay small enough to render.
    if q.per_page > 500:
        raise HTTPException(400, "per_page is limited to 500 (use /api/jobs/export for full dumps)")
    per_page = max(1, q.per_page)
    page = max(1, q.page)
    conn = srv.get_db()
    rows, total = _filtered_jobs(q.model_copy(update={"per_page": per_page, "page": page}))
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

    """Full detail of one saved job."""

    conn = srv.get_db()
    row = job_repo.get_job(conn, job_pk)
    if not row:
        raise HTTPException(404, "Job not found")
    result = dict(row)
    result["history"] = [dict(h) for h in job_repo.get_job_history(conn, job_pk)]
    return result


@router.patch("/api/jobs/{job_pk}/status")
def update_status(job_pk: int, body: StatusUpdate):

    """Change a job pipeline status (Applied, Interviewing, ...)."""
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

    """Set a job note (LLM-extracted or free text)."""
    conn = srv.get_db()
    # B6: /status checked for existence; these two didn't, so a typo'd id got 200
    # and the UI said "saved" over a row that doesn't exist.
    if not job_repo.get_job(conn, job_pk):
        raise HTTPException(404, "Job not found")
    job_repo.update_notes(conn, job_pk, body.notes)
    return {"ok": True}


@router.patch("/api/jobs/{job_pk}/follow-up")
def update_follow_up(job_pk: int, body: FollowUpUpdate):

    """Set the follow-up date for a job."""
    conn = srv.get_db()
    if not job_repo.get_job(conn, job_pk):
        raise HTTPException(404, "Job not found")
    job_repo.update_follow_up(conn, job_pk, body.follow_up)
    return {"ok": True}


@router.post("/api/jobs/{job_pk}/recheck")
def recheck_job(job_pk: int):

    """Re-fetch one job's posting and apply whatever changed (P5) — the drawer button that replaces "run a whole-table Check"."""

    from scraper.parsers import parse_job_detail
    from app import pipeline_apply

    conn = srv.get_db()
    row = job_repo.get_job(conn, job_pk)
    if not row:
        raise HTTPException(404, "Job not found")
    if not row["job_url"]:
        raise HTTPException(400, "This job has no URL to re-check")
    client = srv.get_client()
    try:
        resp = client.get(row["job_url"])
    except Exception as exc:
        raise HTTPException(502, f"Site unreachable ({type(exc).__name__}): {exc}")
    if resp.status_code in (404, 410):
        d = {"row_id": job_pk, "is_closed": True,
             "close_reason": f"{resp.status_code} — job removed from site"}
    else:
        detail = parse_job_detail(resp.text, url=row["job_url"])
        d = {"row_id": job_pk, "title": detail.title, "company": detail.company,
             "description": detail.description, "salary": detail.salary,
             "hours_per_week": detail.hours_per_week, "work_type": detail.work_type,
             "date_updated": detail.date_updated, "skills": detail.skills,
             "category": detail.category, "employer_id": detail.employer_id,
             "is_closed": detail.is_closed, "close_reason": detail.close_reason}
    if not pipeline_apply.apply_enrich(conn, d):
        raise HTTPException(502, "The page parsed no job — the site may have changed")
    # enrich_job bumps jobs_version itself, so the ATS cache re-scores this row.
    return {"ok": True, "is_closed": d.get("is_closed", False),
            "title": d.get("title"), "has_description": bool(d.get("description")),
            "company": d.get("company")}


@router.get("/api/stats")
def stats():

    """Dashboard counters: totals, follow-ups due, scrape health."""

    return job_repo.get_stats(srv.get_db())


@router.post("/api/jobs/reset")
def reset_jobs():

    """Soft-delete every saved job (P6): the rows are stamped, not deleted, so the toolbar's Undo gives them back."""
    conn = srv.get_db()
    try:
        # P6: `deleted_at` existed, was indexed, and nothing used it. A reset that
        # DELETEs 1,232 rows and their history is not a button you get back from.
        n = conn.execute("UPDATE jobs SET deleted_at = datetime('now') "
                         "WHERE deleted_at IS NULL").rowcount
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise HTTPException(
            503, f"Database busy ({exc}) — the pipeline may be writing; try again in a moment"
        )
    events_hub.publish("jobs_reset", {"deleted_jobs": n})
    return {"deleted_jobs": n, "undo": True}


@router.post("/api/jobs/reset/undo")
def undo_reset_jobs():

    """Bring back everything the last reset hid (P6)."""
    conn = srv.get_db()
    n = conn.execute("UPDATE jobs SET deleted_at = NULL WHERE deleted_at IS NOT NULL").rowcount
    conn.commit()
    events_hub.publish("jobs_reset", {"restored_jobs": n})
    return {"restored_jobs": n}
