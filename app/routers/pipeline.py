"""app/routers/pipeline.py — pipeline run/check/stop (SSE streams), keyword
auto-hide rules, auto-run schedule, scrape scope. (W5.1 split; no behaviour
change.)"""

import json
import sqlite3

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from scraper.client import StopToken
from app.schemas import (CheckRequest, KeywordFilter, PipelineRequest,
                         ScrapeScope, StopRequest)
from app.sse import hardened, sse
from app import scheduler
from app import pipeline_apply
from app.services import keywords as kw
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from app import server as srv

router = APIRouter()

# /api/schedule lives in app/routers/settings.py


@router.post("/api/pipeline/run")
def run_pipeline(body: PipelineRequest):
    """Full pipeline: harvest → enrich. Streams SSE."""
    conn = srv.get_db()
    client = srv.get_client()

    # W2.8: this run's own stop token (the shared client is persistent, but
    # stopping is per run). The client is pointed at it only while this run
    # owns the pipeline lock, inside generate().
    run_id = scheduler.new_run_id()
    token = StopToken()

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
                api_skills = srv.fetch_skills(client, keyword="")
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
                srv.log.warning(f"Skill ID lookup failed: {exc}")
        if skill_ids:
            srv.log.info(f"Resolved {len(skill_ids)} skill IDs: {skill_ids}")

    existing_ids = job_repo.get_existing_job_ids(conn)

    def generate():
        # Shared lock with the auto-run scheduler: one scrape at a time.
        if not scheduler.pipeline_lock.acquire(blocking=False):
            yield sse("error", "Another run is in progress (auto-run or another tab) — try again shortly")
            yield sse("done", "busy")
            return
        # W2.6: cross-process guard — a second Job Hunter instance sharing this
        # DB would otherwise fire a second scrape at the site.
        try:
            ok, msg = srv._claim_run_or_busy(conn)
        except Exception as exc:  # e.g. a stale implicit transaction
            scheduler.pipeline_lock.release()
            yield sse("error", f"could not claim the pipeline lock: {exc}")
            yield sse("done", "busy")
            return
        if not ok:
            yield sse("error", f"{msg} — try again shortly")
            yield sse("done", "busy")
            scheduler.pipeline_lock.release()
            return
        # W2.8: this run owns the pipeline now — point the shared client at
        # this run's token, so Stop reaches exactly this run.
        scheduler.begin_run(run_id, token, client)
        try:
            new_job_ids: list[int] = []
            for event in srv.harvest(
                client,
                keyword=keyword,
                categories=categories or None,
                skill_ids=skill_ids or None,
                posted_since=posted_since,
                existing_ids=existing_ids,
            ):
                settings_repo.heartbeat_instance_lock(conn)
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

            if token.stopped:
                scheduler.record_run_status(conn, stopped=True)
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
                workers = srv._cfg.get("enrich_workers", 3)
                for event in srv.enrich(client, jobs_to_enrich, workers=workers):
                    settings_repo.heartbeat_instance_lock(conn)
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

            stopped = token.stopped
            scheduler.record_run_status(conn, stopped=stopped)
            yield sse("done", "stopped" if stopped else "complete")
        except Exception as exc:
            scheduler.record_run_status(conn, error=str(exc))
            raise
        finally:
            settings_repo.release_instance_lock(conn)
            scheduler.end_run(run_id)
            scheduler.pipeline_lock.release()

    return StreamingResponse(hardened(generate(), "pipeline/run", run_id), media_type="text/event-stream")


@router.post("/api/pipeline/check")
def run_check(body: CheckRequest):
    """Re-check existing jobs."""
    conn = srv.get_db()
    client = srv.get_client()

    # W2.8: per-run stop token (same lifecycle as /api/pipeline/run).
    run_id = scheduler.new_run_id()
    token = StopToken()

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
    base = (srv._cfg.get("base_url") or "").strip()
    if base:
        prefix = base.rstrip('/') + '/'
        jobs_to_check = [t for t in jobs_to_check if t[1].startswith(prefix)]

    def generate():
        if not scheduler.pipeline_lock.acquire(blocking=False):
            yield sse("error", "Another run is in progress (auto-run or another tab) — try again shortly")
            yield sse("done", "busy")
            return
        # W2.6: cross-process guard (same as /api/pipeline/run)
        try:
            ok, msg = srv._claim_run_or_busy(conn)
        except Exception as exc:
            scheduler.pipeline_lock.release()
            yield sse("error", f"could not claim the pipeline lock: {exc}")
            yield sse("done", "busy")
            return
        if not ok:
            yield sse("error", f"{msg} — try again shortly")
            yield sse("done", "busy")
            scheduler.pipeline_lock.release()
            return
        # W2.8: this run owns the pipeline now — point the shared client at
        # this run's token, so Stop reaches exactly this run.
        scheduler.begin_run(run_id, token, client)
        try:
            if not jobs_to_check:
                yield sse("log", "No jobs need checking")
                yield sse("done", "nothing to check")
                return

            workers = body.workers or srv._cfg.get("enrich_workers", 3)
            for event in srv.enrich(client, jobs_to_check, workers=workers):
                settings_repo.heartbeat_instance_lock(conn)
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
            stopped = token.stopped
            scheduler.record_run_status(conn, stopped=stopped)
            yield sse("done", "stopped" if stopped else "complete")
        except Exception as exc:
            scheduler.record_run_status(conn, error=str(exc))
            raise
        finally:
            settings_repo.release_instance_lock(conn)
            scheduler.end_run(run_id)
            scheduler.pipeline_lock.release()

    return StreamingResponse(hardened(generate(), "pipeline/check", run_id), media_type="text/event-stream")


@router.post("/api/pipeline/stop")
def stop_pipeline(body: StopRequest = None):
    """W2.8: stop one run. With a run_id, only that run's stop flag is
    flipped; without, the server's active run (backward-compatible)."""
    run_id = body.run_id if body is not None else None
    stopped = scheduler.stop_run(run_id)
    return {"ok": True,
            "message": "Stop signal sent" if stopped else "No active run to stop"}


@router.post("/api/keywords/apply")
def apply_keywords(body: KeywordFilter):
    """
    Apply positive/negative keyword filters to all jobs.
    Positive: hide jobs that DON'T match any positive keyword.
    Negative: hide jobs that DO match any negative keyword.

    Reversible — see app/services/keywords.py for the full rule set.
    """
    positive = [k.strip() for k in body.positive if k.strip()]
    negative = [k.strip() for k in body.negative if k.strip()]
    # Note: applying with NO keywords is not a no-op — it means "no rule hides
    # anything", so every filter-hidden job is restored. That's the
    # "I changed my mind" path; the restore flag forces the same outcome
    # even when keywords are present.

    conn = srv.get_db()
    try:
        # Persist the rules: the auto-run re-applies them after every harvest,
        # and the UI hydrates its inputs from GET /api/keywords on load.
        settings_repo.set(conn, "positive_keywords", json.dumps(positive))
        settings_repo.set(conn, "negative_keywords", json.dumps(negative))
        neg_hidden, pos_hidden, restored = kw.apply_keyword_filters(
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
        "still_filter_hidden": kw.filter_hidden_count(conn),
    }


@router.get("/api/keywords")
def get_keywords():
    """Saved auto-hide rules (for UI hydration) + how many jobs they hide."""
    conn = srv.get_db()
    return {
        "positive": json.loads(settings_repo.get(conn, "positive_keywords", "[]") or "[]"),
        "negative": json.loads(settings_repo.get(conn, "negative_keywords", "[]") or "[]"),
        "still_filter_hidden": kw.filter_hidden_count(conn),
    }


@router.get("/api/scrape-scope")
def get_scrape_scope():
    """What the auto-run harvests. All empty = scrape everything."""
    conn = srv.get_db()
    return {
        "keyword": settings_repo.get(conn, "scrape_keyword", "") or "",
        "categories": json.loads(settings_repo.get(conn, "scrape_categories", "[]") or "[]"),
        "skills": json.loads(settings_repo.get(conn, "scrape_skills", "[]") or "[]"),
    }


@router.post("/api/scrape-scope")
def save_scrape_scope(body: ScrapeScope):
    conn = srv.get_db()
    settings_repo.set(conn, "scrape_keyword", (body.keyword or "").strip())
    settings_repo.set(conn, "scrape_categories", json.dumps(body.categories or []))
    settings_repo.set(conn, "scrape_skills", json.dumps(body.skills or []))
    conn.commit()
    return get_scrape_scope()
