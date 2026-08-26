"""
app/server.py — FastAPI application with all API routes.

Run:  uvicorn app.server:app --reload
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Request

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
from scraper.client import OJClient, ScrapeStopped
from scraper.pipeline import enrich, harvest
from scraper.skills import fetch_skills, skills_to_db_rows, top_level_categories

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Job Hunter")

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Load config
_cfg_path = BASE_DIR / "config.json"
_cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}

# Shared scraper client (for stop control)
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
):
    conn = get_db()
    rows, total = job_repo.get_jobs(
        conn, page=page, per_page=per_page, status=status,
        search=search, include_hidden=include_hidden,
        work_type=work_type, skill=skill,
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
    """Re-fetch all skills from the API and update the DB."""
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
    category = body.category
    posted_since = body.posted_since.isoformat() if body.posted_since else None

    if not keyword and not category:
        raise HTTPException(400, "Keyword or category is required")

    existing_ids = job_repo.get_existing_job_ids(conn)

    def generate():
        # Phase 1: Harvest
        new_job_ids: list[int] = []
        for event in harvest(
            client,
            keyword=keyword,
            category=category,
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
                        search_category=category or None,
                    )
                    if is_new:
                        inserted += 1
                        if stub_data.get("job_id"):
                            new_job_ids.append(row_id)
                yield sse("harvest_done", {"inserted": inserted, "total": len(event.data.get("stubs", []))})
            elif event.type == "summary":
                yield sse("harvest_summary", event.data)

        if client.stopped:
            yield sse("done", "stopped")
            return

        # Phase 2: Enrich new jobs
        if new_job_ids:
            # Get URLs for the new jobs
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

    from fastapi.responses import StreamingResponse
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/pipeline/check")
def run_check(body: CheckRequest):
    """Re-check existing jobs (by default: New/Open status, or all if recheck_all)."""
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

    from fastapi.responses import StreamingResponse
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/pipeline/stop")
def stop_pipeline():
    if _client:
        _client.stop()
    return {"ok": True, "message": "Stop signal sent"}


# ── Keyword filters ─────────────────────────────────────────────────────────

@app.post("/api/keywords/apply")
def apply_keywords(body: KeywordFilter):
    conn = get_db()
    neg_hidden = _hide_by_negative(conn, body.negative)
    pos_hidden = _hide_by_positive(conn, body.positive)
    return {
        "hidden_by_negative": neg_hidden,
        "hidden_by_positive": pos_hidden,
        "total_hidden": neg_hidden + pos_hidden,
    }


def _hide_by_negative(conn, keywords: list[str]) -> int:
    if not keywords:
        return 0
    rows = conn.execute(
        "SELECT id, title, description, company FROM jobs WHERE status != 'Hidden'"
    ).fetchall()
    updated = 0
    for row in rows:
        haystack = " ".join([row["title"] or "", row["description"] or "", row["company"] or ""]).lower()
        if any(kw.lower() in haystack for kw in keywords):
            old = row["status"]
            conn.execute("UPDATE jobs SET status = 'Hidden' WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, ?, 'Hidden')",
                (row["id"], old),
            )
            updated += 1
    conn.commit()
    return updated


def _hide_by_positive(conn, keywords: list[str]) -> int:
    if not keywords:
        return 0
    rows = conn.execute(
        "SELECT id, title, description, company FROM jobs WHERE status = 'New'"
    ).fetchall()
    updated = 0
    for row in rows:
        haystack = " ".join([row["title"] or "", row["description"] or "", row["company"] or ""]).lower()
        if not any(kw.lower() in haystack for kw in keywords):
            conn.execute("UPDATE jobs SET status = 'Hidden' WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, 'New', 'Hidden')",
                (row["id"],),
            )
            updated += 1
    conn.commit()
    return updated
