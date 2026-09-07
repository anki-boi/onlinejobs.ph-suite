"""
app/server.py — FastAPI application with all API routes.

Run:  uvicorn app.server:app --reload
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    CheckRequest,
    FollowUpUpdate,
    KeywordFilter,
    NotesUpdate,
    PipelineRequest,
    ResumeUpdate,
    ScrapeScope,
    ScheduleUpdate,
    StatusUpdate,
    TailorRequest,
)
from app.sse import sse
from app import events as events_hub
from app import pipeline_apply
from app import scheduler
import db.connection as dbconn
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from db.repos import skills as skill_repo
from resumes import ats as resume_ats_mod
from resumes import digest as resume_digest
from resumes import render as resume_render
from resumes import schema as resume_schema
from resumes import yamlcv as resume_yamlcv
from resumes.schema import load_master, save_master, validate
from resumes.tailor import LLMClient, job_brief, tailor
from scraper.client import OJClient
from scraper.pipeline import enrich, harvest
from scraper.skills import fetch_skills, skills_to_db_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Job Hunter")

STATIC_DIR = dbconn.BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_cfg_path = dbconn.BASE_DIR / "config.json"
_cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
_local_cfg = dbconn.BASE_DIR / "config.local.json"   # gitignored overlay (keys etc.)
if _local_cfg.exists():
    _cfg = {**_cfg, **json.loads(_local_cfg.read_text())}

_client: OJClient | None = None
RESUME_PATH = dbconn.BASE_DIR / "resumes" / "master.json"
MASTERS_PATH = dbconn.BASE_DIR / "resumes" / "masters.json"
BUILT_DIR = dbconn.BASE_DIR / "resumes" / "built"
BASE_RESUMES = dbconn.BASE_DIR / "resumes"


def _masters() -> dict:
    return resume_schema.load_masters(MASTERS_PATH)

# ponytail: per-process memo of best-profile ATS per job; invalidated when the
# DB or masters file changes. 2,800 rows ≈ 5.5s cold, ~0s warm.
_ats_memo: dict[int, tuple] = {}
_ats_memo_key = None

def _best_ats_for_row(conn, r) -> tuple:
    global _ats_memo_key
    # ponytail: mtime bumps on every WAL write, so key on row shape instead —
    # scores may lag in-place enrichment updates until a job row is added/deleted.
    row = tuple(conn.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM jobs").fetchone())
    key = (row,
           MASTERS_PATH.stat().st_mtime if MASTERS_PATH.exists() else 0,)
    if key != _ats_memo_key:
        _ats_memo.clear()
        _ats_memo_key = key
    if r["id"] not in _ats_memo:
        _ats_memo[r["id"]] = resume_schema.best_profile_for_job(_masters(), _job_dict_for_resume(r))
    return _ats_memo[r["id"]] 

# In-process cache of (profile, total_score) per job for the min_ats filter.
# ponytail: rebuilt when the DB file or masters file changes; jobs updated in
# place (enrichment) only invalidate via DB mtime — good enough for a filter.
_ats_cache: dict[int, tuple] = {}
_ats_cache_key = None

def _ats_cache_refresh(conn) -> None:
    global _ats_cache_key
    key = (dbconn._resolve_db_path().stat().st_mtime, MASTERS_PATH.stat().st_mtime if MASTERS_PATH.exists() else 0,
           conn.execute("SELECT COALESCE(MAX(id),0) FROM jobs").fetchone()[0])
    if key != _ats_cache_key:
        _ats_cache = {}
        _ats_cache_key = key

def _best_for_row(r) -> tuple:
    """(profile_name, score) — best of all master profiles, memoised."""
    if r["id"] not in _ats_cache:
        _ats_cache[r["id"]] = resume_schema.best_profile_for_job(_masters(), _job_dict_for_resume(r))
    return _ats_cache[r["id"]]

# Default UA — a missing/empty config value must never override it with "".
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def get_client() -> OJClient:
    global _client
    if _client is None:
        _client = OJClient(
            base_url=_cfg.get("base_url", "https://www.onlinejobs.ph"),
            api_url=_cfg.get("api_url", "https://api.onlinejobs.ph"),
            delay=_cfg.get("request_delay", 1.0),
            max_retries=_cfg.get("max_retries", 3),
            user_agent=_cfg.get("user_agent") or _DEFAULT_UA,
        )
    return _client

_initialized_for: str | None = None


def get_db() -> sqlite3.Connection:
    """Per-request connection. Schema init happens once per process per DB
    path (re-checked so a changed JOBS_DB_PATH re-initialises)."""
    global _initialized_for
    key = str(dbconn._resolve_db_path())
    if _initialized_for != key:
        _initialized_for = key
        dbconn.init_db(dbconn.get_conn())
    return dbconn.get_conn()


# ── Health check ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Lightweight liveness probe — no DB call needed."""
    return {"status": "ok", "pid": __import__("os").getpid()}


# ── HTML ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── CSV export (server-side streaming) ──────────────────────────────────────

import csv as _csv
import io as _io

_EXPORT_COLS = ["id", "job_id", "title", "company", "description", "salary",
                "location", "hours_per_week", "work_type", "posted_date",
                "date_updated", "skills", "status", "notes", "follow_up"]


@app.get("/api/jobs/export")
def export_jobs():
    """Stream the full job set as CSV. No pagination — one complete dump."""
    conn = get_db()
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
    posted_from: str | None = None,  # inclusive range start (YYYY-MM-DD) on the displayed posted date
    posted_to: str | None = None,    # inclusive range end
    has_salary: bool = False,
    min_ats: int = 0,  # hide jobs whose best-profile ATS score is below this
):
    conn = get_db()
    rows, total = job_repo.get_jobs(
        conn, page=page, per_page=per_page, status=status,
        search=search, include_hidden=include_hidden,
        work_type=work_type, skill=skill, skills=skills,
        scrape_status=scrape_status, sort=sort, order=order,
        title=title, company=company, salary=salary,
        location=location, hours=hours,
        posted_from=posted_from, posted_to=posted_to,
        has_salary=has_salary,
    )
    if min_ats:
        kept = []
        for r in rows:
            _, sc = _best_ats_for_row(conn, r)
            if sc["total"] >= min_ats:
                kept.append(r)
        # ponytail: filters the fetched page (UI loads per_page=99999 = full set);
        # small per_page + min_ats would be per-page, not global.
        rows, total = kept, len(kept)
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


# ── Auto-run ────────────────────────────────────────────────────────────────

@app.get("/api/schedule")
def get_schedule():
    from db.repos import settings as settings_repo
    conn = get_db()
    return {
        "enabled": settings_repo.get(conn, "auto_run_enabled", "1") == "1",
        "interval_hours": int(settings_repo.get(conn, "auto_run_interval_hours",
                                                str(scheduler.DEFAULT_INTERVAL_HOURS))
                              or scheduler.DEFAULT_INTERVAL_HOURS),
        "last_run": settings_repo.get(conn, "last_run", ""),
        "last_error": settings_repo.get(conn, "last_error", ""),
        "next_run": settings_repo.get(conn, "next_run", ""),
        "running": scheduler.is_running(),
    }


@app.post("/api/schedule")
def set_schedule(body: ScheduleUpdate):
    from db.repos import settings as settings_repo
    conn = get_db()
    if body.enabled is not None:
        settings_repo.set(conn, "auto_run_enabled", "1" if body.enabled else "0")
    if body.interval_hours is not None:
        if not 1 <= body.interval_hours <= 24:
            raise HTTPException(400, "interval_hours must be 1-24")
        settings_repo.set(conn, "auto_run_interval_hours", str(body.interval_hours))
    return get_schedule()


# ── Resume tailoring + ATS ───────────────────────────────────────────────

def _job_dict_for_resume(row) -> dict:
    return {
        "title": row["title"],
        "employer": row["company"],
        "skills": row["skills"],
        "keywords": row["search_keyword"],
        "salary": row["salary"],
        "description": row["description"],
    }


@app.get("/api/resume")
def get_resume(profile: str = ""):
    try:
        return resume_schema.get_profile(_masters(), profile)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")


@app.get("/api/resume/profiles")
def get_resume_profiles():
    d = _masters()
    return {"default": d.get("default"), "profiles": list((d.get("profiles") or {}).keys())}


@app.put("/api/resume")
def put_resume(body: ResumeUpdate):
    errs = validate(body.master)
    if errs:
        raise HTTPException(400, "; ".join(errs))
    d = _masters()
    name = (body.profile or d.get("default") or "master").strip()
    d.setdefault("profiles", {})[name] = body.master
    resume_schema.save_masters(MASTERS_PATH, d)
    return body.master


@app.get("/api/resume/ats")
def resume_ats(job_id: int, profile: str = "", auto: int = 0):
    """Deterministic ATS-style score vs one job. auto=1 → best-fitting profile."""
    conn = get_db()
    row = job_repo.get_job(conn, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = _job_dict_for_resume(row)
    d = _masters()
    if auto:
        name, score = resume_schema.best_profile_for_job(d, jd)
        return {"profile": name, **score}
    try:
        doc = resume_schema.get_profile(d, profile)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    return resume_ats_mod.score_resume(resume_render.to_txt(doc), jd)


@app.post("/api/resume/tailor")
def resume_tailor(body: TailorRequest):
    """LLM-tailored resume for one job + its ATS score. 503 if no LLM configured."""
    llm = LLMClient.from_config(_cfg)
    if llm is None:
        raise HTTPException(503, "No LLM configured — add llm_base_url/llm_model to config.json")
    conn = get_db()
    row = job_repo.get_job(conn, body.job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = _job_dict_for_resume(row)
    d = _masters()
    name = resume_schema.best_profile_for_job(d, jd)[0] if body.auto \
        else (body.profile or d.get("default") or "").strip()
    try:
        doc = resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    tailored = tailor(doc, jd, llm)
    return {
        "profile": name,
        "tailored": tailored,
        "changed": tailored != doc,
        "score": resume_ats_mod.score_resume(resume_render.to_txt(tailored), jd),
    }


@app.get("/api/resume/export")
def resume_export(job_id: int, fmt: str = "docx", tailored: int = 0, profile: str = "", auto: int = 0):
    conn = get_db()
    row = job_repo.get_job(conn, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = _job_dict_for_resume(row)
    d = _masters()
    if auto:
        name = resume_schema.best_profile_for_job(d, jd)[0]
    else:
        name = profile or d.get("default")
    try:
        m = resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    if tailored:
        llm = LLMClient.from_config(_cfg)
        if llm is None:
            raise HTTPException(503, "No LLM configured — add llm_base_url/llm_model to config.json")
        m = tailor(m, jd, llm)
    if fmt == "txt":
        body, ctype, ext = resume_render.to_txt(m), "text/plain; charset=utf-8", "txt"
    else:
        body, ctype, ext = resume_render.to_docx(m), \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    safe = re.sub(r"[^A-Za-z0-9-]+", "_", row["title"] or "resume")[:40]
    from fastapi.responses import Response
    return Response(content=body, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'})


@app.get("/api/resume/yamlcv-status")
def yamlcv_status():
    """Whether the rendercv toolchain is installed (gates the UI button)."""
    return {"available": resume_yamlcv.available()}


@app.get("/api/resume/built")
def built_list():
    if not BUILT_DIR.is_dir():
        return {"items": []}
    items = []
    for f in sorted(BUILT_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        y = f.with_suffix(".yaml")
        items.append({
            "name": f.name,
            "mtime": f.stat().st_mtime,
            "has_yaml": y.exists(),
            "url": f"/api/resume/built/{f.name}",
        })
    return {"items": items}


@app.get("/api/resume/built/{name}")
def built_file(name: str):
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    f = BUILT_DIR / name
    if not f.is_file():
        raise HTTPException(404, "not found")
    from fastapi.responses import FileResponse
    return FileResponse(f, media_type="application/pdf")


@app.post("/api/resume/build")
def resume_build(body: TailorRequest):
    """Digest resume sources + LLM draft + render-loop until exactly 1 page.
    503 if no LLM or no rendercv toolchain; 422 if the loop can't fit a page."""
    if not resume_yamlcv.available():
        raise HTTPException(503, "rendercv toolchain missing - run install.bat (needs Python 3.12+)")
    llm = LLMClient.from_config(_cfg)
    if llm is None:
        raise HTTPException(503, "No LLM configured - add llm_base_url/llm_model to config.local.json")
    conn = get_db()
    row = job_repo.get_job(conn, body.job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = _job_dict_for_resume(row)
    d = _masters()
    name = resume_schema.best_profile_for_job(d, jd)[0] if body.auto \
        else (body.profile or d.get("default") or "").strip()
    try:
        doc = resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    identity = resume_render.to_txt(doc)
    sources = _cfg.get("resume_sources") or [
        r"C:\Users\PC\Dropbox\Resumes", str(BASE_RESUMES)]
    corpus = resume_digest.digest(sources)
    if corpus["chars"] < 200:
        raise HTTPException(400, "No readable resume source files found (set resume_sources in config.local.json)")
    import time as _time
    import uuid as _uuid
    BUILT_DIR.mkdir(exist_ok=True)
    base = f"{re.sub(r'[^A-Za-z0-9-]+', '_', row['title'] or 'cv')[:36]}_{_time.strftime('%Y%m%d-%H%M%S')}_{_uuid.uuid4().hex[:6]}"
    out_dir = BUILT_DIR / f"{base}_work"
    res = resume_yamlcv.build_one_pager(
        llm, resume_digest.corpus_text(corpus), identity, job_brief(jd), out_dir)
    final = BUILT_DIR / f"{base}.pdf"
    if res["pdf"]:
        Path(res["pdf"]).replace(final)
    ym = BUILT_DIR / f"{base}.yaml"
    ym.write_text(res["yaml"], encoding="utf-8")
    if not res["ok"]:
        last = res["history"][-1] if res.get("history") else {}
        tail = f" (last attempt: {last.get('pages', '?')} pages)" if last.get("pages") else ""
        raise HTTPException(422, f"{res.get('error') or 'could not fit one page'}{tail}")
    return {
        "profile": name,
        "ok": True,
        "rounds": res["rounds"],
        "name": final.name,
        "url": f"/api/resume/built/{final.name}",
        "yaml": res["yaml"],
        "history": res["history"],
    }


@app.get("/api/events")
def events_stream():
    """Server-pushed events (auto-run alerts, new jobs). One open stream per tab;
    browsers auto-reconnect, the hub drops dead subscribers."""
    import queue as _queue
    q = events_hub.subscribe()

    def generate():
        try:
            yield sse("connected", "")
            while True:
                try:
                    yield q.get(timeout=15)
                except _queue.Empty:
                    yield sse("ping", "")  # keep proxies/tabs from dropping us
        finally:
            events_hub.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


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

    # Resolve skill names to OJ.ph IDs — from the local skill_tags table first
    # (same data the UI lists); the API is only consulted for names the DB
    # doesn't know (avoids a full skills fetch on every run).
    skill_ids: list[int] = []
    if body.skills:
        db_rows = conn.execute("SELECT id, name FROM skill_tags").fetchall()
        lookup = {r["name"].lower(): r["id"] for r in db_rows}
        unresolved: list[str] = []
        for name in body.skills:
            oid = lookup.get(name.lower())
            if oid is not None and oid not in skill_ids:
                skill_ids.append(oid)
            else:
                unresolved.append(name)
        if unresolved:
            try:
                api_skills = fetch_skills(client, keyword="")
                api_lookup = {
                    (s.get("name") or "").lower(): s.get("id")
                    for s in api_skills
                }
                for name in unresolved:
                    oid = api_lookup.get(name.lower())
                    if oid is None:  # fuzzy: name appears in a known skill name
                        oid = next((v for k, v in api_lookup.items() if name.lower() in k), None)
                    if oid is not None and oid not in skill_ids:
                        skill_ids.append(oid)
            except Exception as exc:
                log.warning(f"Skill ID lookup failed: {exc}")
        if skill_ids:
            log.info(f"Resolved {len(skill_ids)} skill IDs: {skill_ids}")

    existing_ids = job_repo.get_existing_job_ids(conn)

    def generate():
        # Shared lock with the auto-run scheduler: one scrape at a time.
        if not scheduler.pipeline_lock.acquire(blocking=False):
            yield sse("error", "Another run is in progress (auto-run or another tab) — try again shortly")
            yield sse("done", "busy")
            return
        try:
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
                    inserted, new_items = pipeline_apply.apply_harvest(conn, event)
                    # Emit each new job immediately for real-time UI
                    for stub_data, row_id in new_items:
                        if stub_data.get("job_id"):
                            new_job_ids.append(row_id)
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
            else:
                jobs_to_enrich = []
                yield sse("log", "No new jobs — skipping enrichment")

            if jobs_to_enrich:
                workers = _cfg.get("enrich_workers", 3)
                for event in enrich(client, jobs_to_enrich, workers=workers):
                    if event.type == "log":
                        yield sse("log", event.message)
                    elif event.type == "error":
                        yield sse("error", event.message)
                    elif event.type == "enrich_result":
                        d = event.data
                        if pipeline_apply.apply_enrich(conn, d):
                            pass
                        yield sse("enrich_done", {k: v for k, v in d.items() if k != "description"})
                    elif event.type == "summary":
                        yield sse("enrich_summary", event.data)

            yield sse("done", "complete")
        finally:
            scheduler.pipeline_lock.release()

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

    # Only re-check jobs on the configured site. Stray rows (test fixtures with
    # fake domains like test.com) would otherwise burn retries and error every run.
    base = (_cfg.get("base_url") or "").strip()
    if base:
        prefix = base.rstrip('/') + '/'
        jobs_to_check = [t for t in jobs_to_check if t[1].startswith(prefix)]

    def generate():
        if not scheduler.pipeline_lock.acquire(blocking=False):
            yield sse("error", "Another run is in progress (auto-run or another tab) — try again shortly")
            yield sse("done", "busy")
            return
        try:
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
                    pipeline_apply.apply_enrich(conn, d)
                    yield sse("enrich_done", {k: v for k, v in d.items() if k != "description"})
                elif event.type == "summary":
                    yield sse("enrich_summary", event.data)
            yield sse("done", "complete")
        finally:
            scheduler.pipeline_lock.release()

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
    Apply positive/negative keyword filters to all jobs.
    Positive: hide jobs that DON'T match any positive keyword.
    Negative: hide jobs that DO match any negative keyword.

    Jobs are matched on whatever text is available — title, company, skills,
    and the description once enriched. Fresh (unenriched) stubs are included,
    so a new scrape is filtered immediately, not only after enrichment.
    Keywords match whole words (case-insensitive, simple plurals allowed):
    'AI' matches 'AI', 'AI-powered' but never 'email'/'chain'.
    The positive rule only reaches jobs still in 'New': a job the user has
    moved along (Interested, Applied, …) is never auto-hidden.

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
        # Persist the rules: the auto-run re-applies them after every harvest,
        # and the UI hydrates its inputs from GET /api/keywords on load.
        settings_repo.set(conn, "positive_keywords", json.dumps(positive))
        settings_repo.set(conn, "negative_keywords", json.dumps(negative))
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


@app.get("/api/keywords")
def get_keywords():
    """Saved auto-hide rules (for UI hydration) + how many jobs they hide."""
    conn = get_db()
    return {
        "positive": json.loads(settings_repo.get(conn, "positive_keywords", "[]") or "[]"),
        "negative": json.loads(settings_repo.get(conn, "negative_keywords", "[]") or "[]"),
        "still_filter_hidden": _filter_hidden_count(),
    }


# ── Auto-run scrape scope ──────────────────────────────────────────────────

@app.get("/api/scrape-scope")
def get_scrape_scope():
    """What the auto-run harvests. All empty = scrape everything."""
    conn = get_db()
    return {
        "keyword": settings_repo.get(conn, "scrape_keyword", "") or "",
        "categories": json.loads(settings_repo.get(conn, "scrape_categories", "[]") or "[]"),
        "skills": json.loads(settings_repo.get(conn, "scrape_skills", "[]") or "[]"),
    }


@app.post("/api/scrape-scope")
def save_scrape_scope(body: ScrapeScope):
    conn = get_db()
    settings_repo.set(conn, "scrape_keyword", (body.keyword or "").strip())
    settings_repo.set(conn, "scrape_categories", json.dumps(body.categories or []))
    settings_repo.set(conn, "scrape_skills", json.dumps(body.skills or []))
    conn.commit()
    return get_scrape_scope()


# ── Full reset ─────────────────────────────────────────────────────────────

@app.post("/api/jobs/reset")
def reset_jobs():
    """Delete every job + status history. Keeps: saved keyword rules, scrape
    scope, scheduler settings, resume masters, backups."""
    conn = get_db()
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


def _keyword_regexes(keywords: list[str]) -> list[re.Pattern]:
    """Compile whole-word, case-insensitive matchers for keyword filtering.

    'ai' matches 'AI', 'Ai.', 'AI-powered' (and the simple plural 'ais') but
    not 'email', 'chain', 'maintenance'. Multi-word keywords match as phrases.
    The optional trailing 's' is only added when the keyword ends in a letter
    or digit, so 'call' matches 'calls' but not 'calling'.
    """
    out: list[re.Pattern] = []
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        suffix = r"s?" if k[-1].isalnum() else ""
        out.append(re.compile(rf"(?<!\w){re.escape(k)}{suffix}(?!\w)", re.IGNORECASE))
    return out


def _apply_keyword_filters(conn, positive: list[str], negative: list[str],
                           restore_all: bool = False) -> tuple[int, int, int]:
    """Single pass over all jobs; batched writes; one commit.

    Jobs are matched on the text available to them (title, company, skills,
    description-if-enriched) — unenriched stubs are included, so fresh harvests
    are filtered right away. Keyword matching is whole-word (see
    _keyword_regexes).

    Rules per job:
      hide  — matches any negative keyword; or is 'New' (or was 'New' when
              filter-hidden) and positive keywords are set but none match.
      keep  — already Hidden: left as-is (user-hidden and filter-hidden both stay).
      restore — Hidden with filter_hidden=1 that the rules no longer hide goes
                back to its previous status.
    A job whose status the user manually changed (not Hidden) has its
    filter flag cleared and is treated as user-managed.
    Returns (hidden_by_negative, hidden_by_positive, restored).
    """
    neg_res = _keyword_regexes(negative)
    pos_res = _keyword_regexes(positive)

    rows = conn.execute(
        "SELECT id, status, filter_hidden, pre_filter_status, title, description, company, skills "
        "FROM jobs"
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

        neg_match = any(p.search(haystack) for p in neg_res)
        pos_applies = (
            status == "New"
            or (status == "Hidden" and is_fh and row["pre_filter_status"] == "New")
        )
        pos_match = any(p.search(haystack) for p in pos_res)
        should_hide = neg_match or (pos_applies and bool(pos_res) and not pos_match)

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
