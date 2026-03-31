"""
app.py — FastAPI dashboard backend.

Endpoints:
    GET  /                          → serve index.html
    GET  /api/jobs                  → list jobs (filterable)
    GET  /api/jobs/{id}             → single job detail
    PATCH /api/jobs/{id}/status     → update status
    PATCH /api/jobs/{id}/notes      → update notes
    GET  /api/stats                 → status counts
    GET  /api/tags                  → available skill tags
    POST /api/pipeline/run          → start full pipeline (SSE stream)
    POST /api/pipeline/check        → run check_jobs only (SSE stream)
    POST /api/keywords/apply        → apply positive/negative keyword filters
    GET  /api/tags/refresh          → refresh tag catalogue (SSE stream)

Run:
    pip install fastapi uvicorn requests beautifulsoup4
    uvicorn app:app --reload
"""

import json
import threading
from datetime import date
from pathlib import Path
from typing import Generator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import scraper

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Job Hunter Dashboard")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Support both repository layouts:
# - static/ + templates/ folders (recommended)
# - root-level app.js/style.css/index.html files (fallback)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")

# Single shared DB connection (SQLite is fine for single-user local use)
_conn = None

def get_db():
    global _conn
    if _conn is None:
        _conn = db.get_conn()
        db.init_db(_conn)
    return _conn

# ── HTML ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    template_index = TEMPLATES_DIR / "index.html"
    root_index = BASE_DIR / "index.html"
    if template_index.exists():
        return template_index.read_text(encoding="utf-8")
    if root_index.exists():
        return root_index.read_text(encoding="utf-8")
    raise HTTPException(status_code=500, detail="Could not find index.html")

# ── Jobs API ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(
    include_hidden: bool = False,
    status: str | None = None,
):
    conn    = get_db()
    filters = status.split(",") if status else None
    rows    = db.get_jobs(conn, include_hidden=include_hidden, status_filter=filters)
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    conn = get_db()
    row  = db.get_job(conn, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


class StatusUpdate(BaseModel):
    status: str

@app.patch("/api/jobs/{job_id}/status")
def update_status(job_id: int, body: StatusUpdate):
    conn = get_db()
    if not db.get_job(conn, job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        db.update_job_status(conn, job_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


class NotesUpdate(BaseModel):
    notes: str

@app.patch("/api/jobs/{job_id}/notes")
def update_notes(job_id: int, body: NotesUpdate):
    conn = get_db()
    if not db.get_job(conn, job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job_notes(conn, job_id, body.notes)
    return {"ok": True}


@app.get("/api/stats")
def get_stats():
    return db.get_stats(get_db())

# ── Tags API ──────────────────────────────────────────────────────────────────

@app.get("/api/tags")
def list_tags():
    rows = db.get_skill_tags(get_db())
    return [dict(r) for r in rows]

# ── SSE helpers ───────────────────────────────────────────────────────────────

def sse_event(data: str, event: str = "message") -> str:
    return f"event: {event}\ndata: {data}\n\n"


def stream_generator(gen: Generator) -> Generator:
    """Wrap a scraper generator into SSE events, writing results to DB."""
    conn = get_db()
    for line in gen:
        if line.startswith("LOG:"):
            yield sse_event(line[4:].strip(), event="log")

        elif line.startswith("RESULT:"):
            stubs = json.loads(line[7:])
            inserted = 0
            for stub in stubs:
                if db.insert_link(conn, stub["job_link"], stub["search_tag"], stub["date_found"]):
                    inserted += 1
            yield sse_event(
                json.dumps({"inserted": inserted, "total": len(stubs)}),
                event="harvest_done"
            )

        elif line.startswith("SUMMARY:"):
            summary = json.loads(line[8:])
            # Write all job details to DB
            for job_id, details in summary.get("results", []):
                db.update_job_details(conn, job_id, details)
            # Strip results from what we send to client (too large)
            summary.pop("results", None)
            yield sse_event(json.dumps(summary), event="check_done")

    yield sse_event("done", event="complete")

# ── Pipeline endpoints ────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    keyword: str = "medical"
    posted_since: date | None = None
    workers: int = 5

@app.post("/api/pipeline/run")
async def run_pipeline(body: PipelineRequest):
    """
    Full pipeline: harvest links → check & fill details.
    Streams SSE log events.
    """
    conn = get_db()

    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Keyword is required")

    existing = db.get_existing_links(conn)
    hidden   = db.get_hidden_links(conn)

    def generate():
        # Phase 1: harvest links
        new_stubs: list[dict] = []
        for line in scraper.harvest_links(keyword, existing, hidden, body.posted_since):
            if line.startswith("LOG:"):
                yield sse_event(line[4:].strip(), event="log")
            elif line.startswith("RESULT:"):
                stubs = json.loads(line[7:])
                inserted = 0
                for stub in stubs:
                    if db.insert_link(conn, stub["job_link"], stub["search_tag"], stub["date_found"]):
                        inserted += 1
                        new_stubs.append(stub)
                yield sse_event(
                    json.dumps({"inserted": inserted, "total": len(stubs)}),
                    event="harvest_done"
                )

        if not new_stubs:
            yield sse_event("done", event="complete")
            return

        # Phase 2: check & fill new jobs
        # Re-query so we have proper IDs
        jobs_to_check = conn.execute(
            f"SELECT id, job_link, status FROM jobs WHERE job_link IN ({','.join('?' * len(new_stubs))})",
            [s["job_link"] for s in new_stubs]
        ).fetchall()
        jobs_list = [(r["id"], r["job_link"], r["status"]) for r in jobs_to_check]

        for line in scraper.check_and_fill_jobs(jobs_list, workers=body.workers):
            if line.startswith("LOG:"):
                yield sse_event(line[4:].strip(), event="log")
            elif line.startswith("SUMMARY:"):
                summary = json.loads(line[8:])
                for job_id, details in summary.get("results", []):
                    db.update_job_details(conn, job_id, details)
                summary.pop("results", None)
                yield sse_event(json.dumps(summary), event="check_done")

        yield sse_event("done", event="complete")

    return StreamingResponse(generate(), media_type="text/event-stream")


class CheckRequest(BaseModel):
    workers: int = 5
    recheck_all: bool = False

@app.post("/api/pipeline/check")
async def run_check(body: CheckRequest):
    """Re-check open/unchecked jobs only (or all if recheck_all=True)."""
    conn = get_db()

    if body.recheck_all:
        rows = conn.execute("SELECT id, job_link, status FROM jobs ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT id, job_link, status FROM jobs WHERE status = 'New' OR status = 'Open' ORDER BY id"
        ).fetchall()

    jobs_list = [(r["id"], r["job_link"], r["status"]) for r in rows]

    def generate():
        for line in scraper.check_and_fill_jobs(jobs_list, workers=body.workers):
            if line.startswith("LOG:"):
                yield sse_event(line[4:].strip(), event="log")
            elif line.startswith("SUMMARY:"):
                summary = json.loads(line[8:])
                for job_id, details in summary.get("results", []):
                    db.update_job_details(conn, job_id, details)
                summary.pop("results", None)
                yield sse_event(json.dumps(summary), event="check_done")
        yield sse_event("done", event="complete")

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Keyword filter endpoint ───────────────────────────────────────────────────

class KeywordFilter(BaseModel):
    positive: list[str] = []
    negative: list[str] = []

@app.post("/api/keywords/apply")
def apply_keywords(body: KeywordFilter):
    conn = get_db()
    neg_hidden = db.hide_jobs_by_keywords(conn, body.negative)
    pos_hidden = db.filter_by_positive_keywords(conn, body.positive)
    return {
        "hidden_by_negative": neg_hidden,
        "hidden_by_positive": pos_hidden,
        "total_hidden": neg_hidden + pos_hidden,
    }

# ── Tag refresh endpoint ──────────────────────────────────────────────────────

@app.get("/api/tags/refresh")
async def refresh_tags():
    def generate():
        yield sse_event("Fetching tag catalogue from OnlineJobs.ph…", event="log")
        tags, logs = scraper.scrape_tags()
        for log in logs:
            yield sse_event(log, event="log")
        if tags:
            conn = get_db()
            count = db.upsert_skill_tags(conn, tags)
            yield sse_event(json.dumps({"count": count}), event="tags_done")
        else:
            yield sse_event(json.dumps({"count": 0, "error": "No tags found"}), event="tags_done")
        yield sse_event("done", event="complete")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Stop pipeline endpoint ────────────────────────────────────────────────────

@app.post("/api/pipeline/stop")
def stop_pipeline():
    """Signal all running scraping operations to stop."""
    scraper.stop_scraping()
    return {"ok": True, "message": "Stop signal sent"}
