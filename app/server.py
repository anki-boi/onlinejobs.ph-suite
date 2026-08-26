"""
app/server.py — FastAPI application with all API routes.

Run:  uvicorn app.server:app --reload
"""

import json
import logging
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    CheckRequest,
    FollowUpUpdate,
    KeywordFilter,
    NotesUpdate,
    PipelineRequest,
    StatusUpdate,
)
from app.sse import sse
from db.connection import init_db, get_conn, BASE_DIR
from db.repos import jobs as job_repo
from db.repos import skills as skill_repo
from scraper.client import OJClient
from scraper.pipeline import enrich, harvest
from scraper.skills import fetch_skills, skills_to_db_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Job Hunter")

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_cfg_path = BASE_DIR / "config.json"
_cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}

_client: OJClient | None = None

def get_client() -> OJClient:
    global _client
    if _client is None:
        _client = OJClient(
            base_url=_cfg.get("base_url", "https://www.onlinejobs.ph"),
            api_url=_cfg.get("api_url", "https://api.onlinejobs.ph"),
            delay=_cfg.get("request_delay", 1.0),
            max_retries=_cfg.get("max_retries", 3),
            user_agent=_cfg.get("user_agent", ""),
        )
    return _client

def get_db():
    return init_db(get_conn())


# ── HTML ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── Jobs API ────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(
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
    posted: str | None = None,
):
    conn = get_db()
    rows, total = job_repo.get_jobs(
        conn, page=page, per_page=per_page, status=status,
        search=search, include_hidden=include_hidden,
        work_type=work_type, skill=skill, skills=skills,
        scrape_status=scrape_status, sort=sort, order=order,
        title=title, company=company, salary=salary,
        location=location, hours=hours, posted=posted,
    )
    return {"jobs": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


@app.get("/api/jobs/{job_pk}")
def get_job(job_pk: int):
    conn = get_db()
    row = job_repo.get_job(conn, job_pk)
    if not row:
        raise HTTPException(404, "Job not found")
    result = dict(row)
    result["history"] = [dict(h) for h in job_repo.get_job_history(conn, job_pk)]
    return result


@app.patch("/api/jobs/{job_pk}/status")
def update_status(job_pk: int, body: StatusUpdate):
    conn = get_db()
    if not job_repo.get_job(conn, job_pk):
        raise HTTPException(404, "Job not found")
    try:
        job_repo.update_status(conn, job_pk, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.patch("/api/jobs/{job_pk}/notes")
def update_notes(job_pk: int, body: NotesUpdate):
    conn = get_db()
    job_repo.update_notes(conn, job_pk, body.notes)
    return {"ok": True}


@app.patch("/api/jobs/{job_pk}/follow-up")
def update_follow_up(job_pk: int, body: FollowUpUpdate):
    conn = get_db()
    job_repo.update_follow_up(conn, job_pk, body.follow_up)
    return {"ok": True}


# ── Stats ───────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    return job_repo.get_stats(get_db())


# ── Skills API ──────────────────────────────────────────────────────────────

@app.get("/api/skills")
def list_skills(search: str | None = None):
    conn = get_db()
    if search:
        rows = skill_repo.search_skills(conn, search)
    else:
        rows = skill_repo.get_all_skills(conn)
    return [dict(r) for r in rows]


@app.get("/api/skills/categories")
def skill_categories():
    conn = get_db()
    return skill_repo.get_categories(conn)


@app.post("/api/skills/refresh")
def refresh_skills():
    client = get_client()
    try:
        skills = fetch_skills(client, keyword="")
    except Exception as exc:
        raise HTTPException(502, f"Skills API error: {exc}")
    rows = skills_to_db_rows(skills)
    conn = get_db()
    count = skill_repo.upsert_skills(conn, rows)
    return {"count": count}


# ── Pipeline ────────────────────────────────────────────────────────────────

@app.post("/api/pipeline/run")
def run_pipeline(body: PipelineRequest):
    """Full pipeline: harvest → enrich. Streams SSE."""
    conn = get_db()
    client = get_client()
    client.reset()

    keyword = (body.keyword or "").strip()
    categories = body.categories or []
    posted_since = body.posted_since.isoformat() if body.posted_since else None

    # Resolve skill names to OJ.ph IDs via the skills API
    skill_ids: list[int] = []
    if body.skills:
        from scraper.skills import fetch_skills
        try:
            all_skills = fetch_skills(client, keyword="")
            # Build a name→id lookup
            lookup = {}
            for s in all_skills:
                lookup[s.get("name", "").lower()] = s.get("id")
            for name in body.skills:
                oid = lookup.get(name.lower())
                if oid:
                    skill_ids.append(oid)
                else:
                    # Fuzzy match
                    for k, v in lookup.items():
                        if name.lower() in k:
                            skill_ids.append(v)
                            break
        except Exception as exc:
            log.warning(f"Skill ID lookup failed: {exc}")
        if skill_ids:
            log.info(f"Resolved {len(skill_ids)} skill IDs: {skill_ids}")

    existing_ids = job_repo.get_existing_job_ids(conn)

    def generate():
        new_job_ids: list[int] = []
        for event in harvest(
            client,
            keyword=keyword,
            categories=categories or None,
            skill_ids=skill_ids or None,
            posted_since=posted_since,
            existing_ids=existing_ids,
        ):
            if event.type == "log":
                yield sse("log", event.message)
            elif event.type == "error":
                yield sse("error", event.message)
            elif event.type == "harvest_result":
                inserted = 0
                for stub_data in event.data.get("stubs", []):
                    row_id, is_new = job_repo.upsert_stub(
                        conn,
                        job_id=stub_data.get("job_id") or 0,
                        job_url=stub_data["job_url"],
                        title=stub_data.get("title"),
                        work_type=stub_data.get("work_type"),
                        company=stub_data.get("company"),
                        posted_date=stub_data.get("posted_date"),
                        salary=stub_data.get("salary"),
                        location=stub_data.get("location"),
                        hours=stub_data.get("hours"),
                        skills=stub_data.get("skills") or None,
                        search_keyword=keyword or None,
                        search_category=event.data.get("category"),
                    )
                    if is_new:
                        inserted += 1
                        if stub_data.get("job_id"):
                            new_job_ids.append(row_id)
                        # Emit each new job immediately for real-time UI
                        yield sse("harvest_stub", {
                            "row_id": row_id,
                            "job_id": stub_data.get("job_id"),
                            "title": stub_data.get("title"),
                            "company": stub_data.get("company"),
                            "work_type": stub_data.get("work_type"),
                            "posted_date": stub_data.get("posted_date"),
                            "salary": stub_data.get("salary"),
                            "skills": stub_data.get("skills"),
                            "job_url": stub_data.get("job_url"),
                        })
                yield sse("harvest_done", {"inserted": inserted, "total": len(event.data.get("stubs", []))})
            elif event.type == "summary":
                yield sse("harvest_summary", event.data)

        if client.stopped:
            yield sse("done", "stopped")
            return

        # Phase 2: Enrich new jobs
        if new_job_ids:
            rows = conn.execute(
                f"SELECT id, job_url FROM jobs WHERE id IN ({','.join('?' * len(new_job_ids))})",
                new_job_ids,
            ).fetchall()
            jobs_to_enrich = [(r["id"], r["job_url"]) for r in rows]

            workers = _cfg.get("enrich_workers", 3)
            for event in enrich(client, jobs_to_enrich, workers=workers):
                if event.type == "log":
                    yield sse("log", event.message)
                elif event.type == "error":
                    yield sse("error", event.message)
                elif event.type == "enrich_result":
                    d = event.data
                    if "error" not in d:
                        job_repo.enrich_job(
                            conn, d["row_id"],
                            title=d.get("title"),
                            company=d.get("company"),
                            description=d.get("description"),
                            salary=d.get("salary"),
                            hours_per_week=d.get("hours_per_week"),
                            work_type=d.get("work_type"),
                            date_updated=d.get("date_updated"),
                            skills=d.get("skills") or None,
                            employer_id=d.get("employer_id"),
                            is_closed=d.get("is_closed", False),
                            close_reason=d.get("close_reason"),
                        )
                    yield sse("enrich_done", {k: v for k, v in d.items() if k != "description"})
                elif event.type == "summary":
                    yield sse("enrich_summary", event.data)

        yield sse("done", "complete")

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/pipeline/check")
def run_check(body: CheckRequest):
    """Re-check existing jobs."""
    conn = get_db()
    client = get_client()
    client.reset()

    if body.recheck_all:
        rows = conn.execute(
            "SELECT id, job_url, status FROM jobs WHERE job_url != '' ORDER BY id"
        ).fetchall()
    else:
        rows = job_repo.get_jobs_needing_enrichment(
            conn, max_age_days=body.max_age_days,
            status_filter=["New", "Interested"] if not body.recheck_all else None,
        )

    jobs_to_check = [(r["id"], r["job_url"]) for r in rows if r["job_url"]]

    def generate():
        if not jobs_to_check:
            yield sse("log", "No jobs need checking")
            yield sse("done", "nothing to check")
            return

        workers = body.workers or _cfg.get("enrich_workers", 3)
        for event in enrich(client, jobs_to_check, workers=workers):
            if event.type == "log":
                yield sse("log", event.message)
            elif event.type == "error":
                yield sse("error", event.message)
            elif event.type == "enrich_result":
                d = event.data
                if "error" not in d:
                    job_repo.enrich_job(
                        conn, d["row_id"],
                        title=d.get("title"),
                        company=d.get("company"),
                        description=d.get("description"),
                        salary=d.get("salary"),
                        hours_per_week=d.get("hours_per_week"),
                        work_type=d.get("work_type"),
                        date_updated=d.get("date_updated"),
                        skills=d.get("skills") or None,
                        employer_id=d.get("employer_id"),
                        is_closed=d.get("is_closed", False),
                        close_reason=d.get("close_reason"),
                    )
                yield sse("enrich_done", {k: v for k, v in d.items() if k != "description"})
            elif event.type == "summary":
                yield sse("enrich_summary", event.data)
        yield sse("done", "complete")

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/pipeline/stop")
def stop_pipeline():
    if _client:
        _client.stop()
    return {"ok": True, "message": "Stop signal sent"}


# ── Keyword filters (post-enrichment) ───────────────────────────────────────

@app.post("/api/keywords/apply")
def apply_keywords(body: KeywordFilter):
    """
    Apply positive/negative keyword filters to ENRICHED jobs only.
    Positive: hide jobs that DON'T match any positive keyword.
    Negative: hide jobs that DO match any negative keyword.

    Reversible: jobs hidden by this action are tagged (filter_hidden=1) with
    their previous status. Re-applying with changed/removed keywords restores
    any filter-hidden job the rules no longer hide. Jobs the user manually
    hid (or manually re-statused) are never auto-restored.
    """
    positive = [k.strip() for k in body.positive if k.strip()]
    negative = [k.strip() for k in body.negative if k.strip()]
    # Note: applying with NO keywords is not a no-op — it means "no rule hides
    # anything", so every filter-hidden job is restored. That's the
    # "I changed my mind" path; the restore flag forces the same outcome
    # even when keywords are present.

    conn = get_db()
    try:
        neg_hidden, pos_hidden, restored = _apply_keyword_filters(
            conn, positive, negative, restore_all=body.restore
        )
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise HTTPException(
            503, f"Database busy ({exc}) — the pipeline may be writing; try again in a moment"
        )
    return {
        "hidden_by_negative": neg_hidden,
        "hidden_by_positive": pos_hidden,
        "total_hidden": neg_hidden + pos_hidden,
        "restored": restored,
        "still_filter_hidden": _filter_hidden_count(),
    }


def _filter_hidden_count() -> int:
    conn = get_db()
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE filter_hidden = 1").fetchone()[0]


def _get_haystack(row) -> str:
    """Build searchable text from all job fields."""
    return " ".join([
        row["title"] or "",
        row["description"] or "",
        row["company"] or "",
        row["skills"] or "",
    ]).lower()


def _apply_keyword_filters(conn, positive: list[str], negative: list[str],
                           restore_all: bool = False) -> tuple[int, int, int]:
    """Single pass over enriched jobs; batched writes; one commit.

    Rules per job (enriched only):
      hide  — matches any negative keyword; or is 'New' (or was 'New' when
              filter-hidden) and positive keywords are set but none match.
      keep  — already Hidden: left as-is (user-hidden and filter-hidden both stay).
      restore — Hidden with filter_hidden=1 that the rules no longer hide goes
                back to its previous status.
    A job whose status the user manually changed (not Hidden) has its
    filter flag cleared and is treated as user-managed.
    Returns (hidden_by_negative, hidden_by_positive, restored).
    """
    neg_kws = [k.lower() for k in negative]
    pos_kws = [k.lower() for k in positive]

    rows = conn.execute(
        "SELECT id, status, filter_hidden, pre_filter_status, title, description, company, skills "
        "FROM jobs WHERE description IS NOT NULL AND description != ''"
    ).fetchall()

    to_hide: list[tuple[int, str]] = []    # (id, previous status)
    to_restore: list[tuple[int, str]] = [] # (id, status to restore)
    flag_clears: list[int] = []
    neg_hidden = pos_hidden = restored = 0

    for row in rows:
        haystack = _get_haystack(row)
        status = row["status"]
        is_fh = bool(row["filter_hidden"])

        if restore_all:
            if status == "Hidden" and is_fh:
                to_restore.append((row["id"], row["pre_filter_status"] or "New"))
                restored += 1
            continue

        neg_match = bool(neg_kws) and any(k in haystack for k in neg_kws)
        pos_applies = (
            status == "New"
            or (status == "Hidden" and is_fh and row["pre_filter_status"] == "New")
        )
        pos_match = bool(pos_kws) and any(k in haystack for k in pos_kws)
        should_hide = neg_match or (pos_applies and bool(pos_kws) and not pos_match)

        if should_hide:
            if status == "Hidden":
                continue  # already hidden — user's or filter's, leave it
            to_hide.append((row["id"], status))
            if neg_match:
                neg_hidden += 1
            else:
                pos_hidden += 1
        else:
            if status == "Hidden" and is_fh:
                to_restore.append((row["id"], row["pre_filter_status"] or "New"))
                restored += 1
            elif is_fh and status != "Hidden":
                # User manually re-statused a filter-hidden job — stop managing it
                flag_clears.append(row["id"])

    if to_hide:
        conn.executemany(
            "UPDATE jobs SET status = 'Hidden', filter_hidden = 1, pre_filter_status = ? WHERE id = ?",
            [(pre, id) for id, pre in to_hide],
        )
        conn.executemany(
            "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, ?, 'Hidden')",
            [(id, pre) for id, pre in to_hide],
        )
    if to_restore:
        conn.executemany(
            "UPDATE jobs SET status = ?, filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
            [(new_status, id) for id, new_status in to_restore],
        )
        conn.executemany(
            "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, 'Hidden', ?)",
            [(id, new_status) for id, new_status in to_restore],
        )
    if flag_clears:
        conn.executemany(
            "UPDATE jobs SET filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
            [(id,) for id in flag_clears],
        )
    if to_hide or to_restore or flag_clears:
        conn.commit()
    return neg_hidden, pos_hidden, restored
