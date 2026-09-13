"""app/routers/pipeline.py — pipeline run/check/stop (SSE streams). (W5.1
split; no behaviour change.) Keyword rules, auto-run schedule and scrape
scope live in app/routers/settings.py."""

from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from scraper.client import StopToken
from app.schemas import CheckRequest, PipelineRequest, StopRequest
from app.sse import hardened, sse
from app import scheduler
from app import pipeline_apply
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from app import server as srv

router = APIRouter()


def _acquire_pipeline(conn, run_id, token, client, scope=None):
    """Take the pipeline for one run. Returns (events, acquired): events are
    the SSE lines to emit when the run could not start (lock or cross-process
    claim busy); begin_run has been called when acquired."""
    if not scheduler.pipeline_lock.acquire(blocking=False):
        return [sse("error", "Another run is in progress (auto-run or another tab) — try again shortly"),
                sse("done", "busy")], False
    # W2.6: cross-process guard — a second instance sharing this DB would double-scrape.
    try:
        ok, msg = srv._claim_run_or_busy(conn)
    except Exception as exc:  # e.g. a stale implicit transaction
        scheduler.pipeline_lock.release()
        return [sse("error", f"could not claim the pipeline lock: {exc}"),
                sse("done", "busy")], False
    if not ok:
        scheduler.pipeline_lock.release()
        return [sse("error", f"{msg} — try again shortly"), sse("done", "busy")], False
    # W2.8: this run owns the pipeline now — Stop reaches exactly this run.
    scheduler.begin_run(run_id, token, client, scope=scope)
    return [], True

# /api/schedule lives in app/routers/settings.py


@router.post("/api/pipeline/run")
def run_pipeline(body: PipelineRequest):
    """Full pipeline: harvest → enrich. Streams SSE."""
    conn = srv.get_db()
    client = srv.get_client()

    # W2.8: per-run stop token; the client is pointed at it only while this run owns the lock.
    run_id = scheduler.new_run_id()
    token = StopToken()

    keyword = (body.keyword or "").strip()
    categories = body.categories or []
    posted_since = body.posted_since.isoformat() if body.posted_since else None
    scope = {  # W5.4: canonical scope of this run (for idempotency + scope tracking)
        "keyword": keyword,
        "categories": list(categories),
        "skills": list(body.skills or []),
        "posted_since": posted_since,
    }

    # Resolve skill names to OJ.ph IDs from the local skill_tags first; the API only covers names the DB doesn't know.
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
                api_lookup = {(s.get("name") or "").lower(): s.get("id") for s in api_skills}
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
        if scheduler.is_running() and scheduler.active_scope() == scope:
            # W5.4 idempotency: an identical run is already in progress —
            # report its run_id instead of failing with busy.
            yield sse("run_id", {"run_id": scheduler.active_run_id,
                                 "status": "already_running"})
            yield sse("done", "already_running")
            return
        events, acquired = _acquire_pipeline(conn, run_id, token, client, scope)
        if not acquired:
            yield from events
            return
        try:
            new_job_ids: list[int] = []
            for event in srv.harvest(client, keyword=keyword, categories=categories or None,
                                     skill_ids=skill_ids or None, posted_since=posted_since,
                                     existing_ids=existing_ids):
                settings_repo.heartbeat_instance_lock(conn)
                if event.type == "log":
                    yield sse("log", event.message)
                elif event.type == "error":
                    yield sse("error", event.message)
                elif event.type == "harvest_result":
                    inserted, new_items = pipeline_apply.apply_harvest(conn, event)
                    for stub_data, row_id in new_items:  # each new job streams out immediately
                        if stub_data.get("job_id"):
                            new_job_ids.append(row_id)
                        yield sse("harvest_stub", {k: stub_data.get(k) for k in
                                                  ("job_id", "title", "company", "work_type",
                                                   "posted_date", "salary", "skills", "job_url")}
                                  | {"row_id": row_id})
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

    # Only re-check jobs on the configured site; stray fixture rows (test.com) would burn retries.
    base = (srv._cfg.get("base_url") or "").strip()
    if base:
        prefix = base.rstrip('/') + '/'
        jobs_to_check = [t for t in jobs_to_check if t[1].startswith(prefix)]

    def generate():
        events, acquired = _acquire_pipeline(conn, run_id, token, client)
        if not acquired:
            yield from events
            return
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
    """W2.8: stop one run (a run_id, or the active run when omitted)."""
    run_id = body.run_id if body is not None else None
    stopped = scheduler.stop_run(run_id)
    return {"ok": True,
            "message": "Stop signal sent" if stopped else "No active run to stop"}
