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

router = APIRouter(tags=['Pipeline'])


def _acquire_pipeline(conn, run_id, token, client, scope=None, kind="harvest"):
    """Take the pipeline for one run; returns (sse_lines_for_a_busy_run, acquired)."""
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
    scheduler.begin_run(run_id, token, client, scope=scope, kind=kind)
    return [], True


@router.post("/api/pipeline/run")
def run_pipeline(body: PipelineRequest):
    """Start a scrape+enrich run as an SSE stream. A POST with the same scope as the run in progress returns that run_id (already_running) instead of busy."""
    conn = srv.get_db()
    client = srv.get_client()

    run_id = scheduler.new_run_id()
    token = StopToken()

    keyword = (body.keyword or "").strip()
    categories = body.categories or []
    posted_since = body.posted_since.isoformat() if body.posted_since else None
    # W5.4: canonical scope of this run (idempotency + scope tracking)
    scope = {"keyword": keyword, "categories": list(categories),
             "skills": list(body.skills or []), "posted_since": posted_since}

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
        if scheduler.is_running() and scheduler.active_scope() == scope:
            # W5.4 idempotency: an identical run is in progress — report its run_id.
            yield sse("run_id", {"run_id": scheduler.active_run_id,
                                 "status": "already_running"})
            yield sse("done", "already_running")
            return
        events, acquired = _acquire_pipeline(conn, run_id, token, client, scope)
        if not acquired:
            yield from events
            return
        try:
            tally = {"new": 0, "seen": 0, "closed": 0, "errors": 0}  # P8: for the runs row
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
                    tally["new"] += inserted
                elif event.type == "summary":
                    tally["new"] = event.data.get("new", tally["new"])
                    tally["seen"] = event.data.get("seen", 0)
                    yield sse("harvest_summary", event.data)

            if token.stopped:
                scheduler.record_run_status(conn, stopped=True, summary=tally)
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
                        pipeline_apply.apply_enrich(conn, d)
                        yield sse("enrich_done", {k: v for k, v in d.items() if k != "description"})
                    elif event.type == "summary":
                        tally["closed"] = event.data.get("closed", 0)
                        tally["errors"] = event.data.get("errors", 0)
                        yield sse("enrich_summary", event.data)
                if not token.stopped:
                    neg_h, pos_h, restored = pipeline_apply.apply_saved_keyword_rules(conn)
                    if neg_h + pos_h + restored:
                        yield sse("log", f"Keyword rules: {neg_h + pos_h} hidden, {restored} restored")

            stopped = token.stopped
            scheduler.record_run_status(conn, stopped=stopped, summary=tally)
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
    """Re-check the posting status of existing jobs (SSE stream)."""
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
        # B7: one definition of stale — config's enrich_interval_days.
        cfg_age = int(srv._cfg.get("enrich_interval_days", 7) or 7)
        max_age = body.max_age_days if body.max_age_days is not None else cfg_age
        rows = job_repo.get_jobs_needing_enrichment(
            conn, max_age_days=max_age,
            status_filter=None if body.recheck_all else ["New", "Interested"],
        )

    jobs_to_check = [(r["id"], r["job_url"]) for r in rows if r["job_url"]]

    # Only re-check jobs on the configured site; stray fixture rows (test.com) would burn retries.
    base = (srv._cfg.get("base_url") or "").rstrip('/')
    if base:
        jobs_to_check = [t for t in jobs_to_check if t[1].startswith(base + '/')]

    def generate():
        events, acquired = _acquire_pipeline(conn, run_id, token, client, kind="check")
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
            if not token.stopped:
                neg_h, pos_h, restored = pipeline_apply.apply_saved_keyword_rules(conn)
                if neg_h + pos_h + restored:
                    yield sse("log", f"Keyword rules: {neg_h + pos_h} hidden, {restored} restored")
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
    """Stop the active run, or the run_id named in the body (W2.8: a run_id, or the active run when omitted)."""
    run_id = body.run_id if body is not None else None
    stopped = scheduler.stop_run(run_id)
    return {"ok": True, "message": "Stop signal sent" if stopped else "No active run to stop"}
