"""
app/scheduler.py — auto-run: harvest + enrich on an interval in a background
thread, publishing alerts (new jobs, closures, salary, follow-ups, structure
changes) via the app.events hub so any open dashboard tab can raise a
desktop notification.

State lives in the app_settings table (survives restarts):
  auto_run_enabled            "1"/"0"       (default on — user chose every 4h)
  auto_run_interval_hours     "1".."24"     (default 4)
  last_run / last_error       for the UI
  last_status                 "completed"/"stopped"/"failed" (W2.8)
  next_run                    epoch seconds; the tick fires when now >= it
"""

import json
import logging
import os
import threading
import time
import uuid

import db.connection as dbconn
from app import events
from app import pipeline_apply
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from scraper.client import StopToken
from scraper.pipeline import enrich, harvest

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 4

# Manual pipeline endpoints take this too, so an auto-run and a hand-pressed
# Run never fire two scrapes at the site at once.
pipeline_lock = threading.Lock()

# W2.8: stop is per-run, not per-client. The OJClient is shared and
# persistent (session reuse across runs), but each run registers its own
# StopToken and the client is pointed at it only while that run holds the
# pipeline lock — so a stop reaches exactly that run and can't leak into the
# next one (the old single shared flag: stopping a manual run left the flag
# set for the auto-run that followed).
active_runs: dict[str, StopToken] = {}
active_run_id: str | None = None


def new_run_id() -> str:
    return uuid.uuid4().hex


def register_run(run_id: str, token: StopToken) -> None:
    """Remember a run's stop token (idempotent; also called by begin_run)."""
    active_runs[run_id] = token


def begin_run(run_id: str, token: StopToken, client) -> None:
    """Called when a run owns the pipeline lock: register the token, mark the
    run active, and point the shared client at it."""
    global active_run_id
    register_run(run_id, token)
    active_run_id = run_id
    client.set_stop(token)


def end_run(run_id: str) -> None:
    """Run finished (complete/stopped/failed): drop its token."""
    global active_run_id
    active_runs.pop(run_id, None)
    if active_run_id == run_id:
        active_run_id = None


def stop_run(run_id: str | None = None) -> bool:
    """Flip the stop flag of one run. An explicit run_id targets exactly that
    run (no fallback — a stale id must not kill a different run); None
    targets the server's active run (backward-compatible stop button).
    Returns True if a token was flipped."""
    if run_id:
        target = active_runs.get(run_id)
    else:
        target = active_runs.get(active_run_id) if active_run_id else None
    if target is not None:
        target.stop()
        return True
    return False


def wait_runs(timeout: float) -> bool:
    """W2.9: wait until every registered run has ended (its finally-block
    released it), or the deadline passes. Returns True if all ended."""
    deadline = time.time() + timeout
    while active_runs:
        if time.time() >= deadline:
            return False
        time.sleep(0.1)
    return True


def record_run_status(conn, stopped: bool = False, error: str | None = None) -> None:
    """W2.8: persist a run's outcome. A stopped run is 'stopped' (with its
    partial results) — never 'failed'. Only a raised error is 'failed'."""
    if error:
        settings_repo.set(conn, "last_error", error)
        settings_repo.set(conn, "last_status", "failed")
    else:
        settings_repo.set(conn, "last_status", "stopped" if stopped else "completed")
        if stopped:
            settings_repo.set(conn, "last_error", "")


def _defaults_conn():
    return dbconn.init_db(dbconn.get_conn())


def start(interval_sec: int = 60, on_tick=None) -> threading.Thread:
    """Spawn the daemon thread. on_tick (default: tick) is retried forever."""
    def loop():
        while True:
            try:
                (on_tick or tick)()
            except Exception as exc:  # the scheduler must never die silently
                log.warning(f"auto-run tick failed: {exc}")
            time.sleep(interval_sec)

    t = threading.Thread(target=loop, daemon=True, name="auto-run")
    t.start()
    return t


def is_running() -> bool:
    return pipeline_lock.locked()


def tick(client_factory=None) -> None:
    """One scheduler heartbeat: due? enabled? free? → run_once."""
    conn = _defaults_conn()
    if settings_repo.get(conn, "auto_run_enabled", "1") != "1":
        return
    next_run = settings_repo.get(conn, "next_run", "")
    if next_run:
        try:
            if time.time() < float(next_run):
                return
        except ValueError:
            pass  # corrupt value → run anyway, the write below repairs it
    if not pipeline_lock.acquire(blocking=False):
        return  # a manual run is in progress — retry next tick
    run_id = None
    try:
        # W2.6: cross-process guard — a second Job Hunter instance with the
        # same DB would otherwise fire a second scrape at the site.
        holder = settings_repo.instance_lock_holder(conn)
        if holder and not holder["stale"] and holder["pid"] != os.getpid():
            log.info(f"another instance is running the pipeline (pid {holder['pid']}) — "
                     "skipping auto-run")
            return
        if not settings_repo.acquire_instance_lock(conn):
            return  # lost the race to another process — retry next tick
        hours = int(settings_repo.get(conn, "auto_run_interval_hours", str(DEFAULT_INTERVAL_HOURS))
                    or DEFAULT_INTERVAL_HOURS)
        settings_repo.set(conn, "last_run", time.strftime("%Y-%m-%d %H:%M:%S"))
        settings_repo.set(conn, "next_run", str(time.time() + hours * 3600))
        settings_repo.set(conn, "last_error", "")
        if client_factory is None:
            from app.server import get_client  # local import: server imports us
            client_factory = get_client
        # W2.5: the live config (app/config.py) — enrich_workers /
        # enrich_interval_days in config.local.json are honored, no restart.
        cfg = dbconn.load_config()
        client = client_factory()
        # W2.8: this run gets its own stop token; the shared client now
        # references it, so a stop can't leak into the next run.
        run_id = new_run_id()
        begin_run(run_id, StopToken(), client)
        summary = run_once(client, conn, events.publish, cfg)
        record_run_status(conn, stopped=client.stopped)
        events.publish("schedule_done", {**summary, "run_id": run_id})
    except Exception as exc:
        log.warning(f"auto-run failed: {exc}")
        record_run_status(conn, error=str(exc))
        events.publish("alert", {"type": "error", "message": f"auto-run failed: {exc}"})
    finally:
        if run_id is not None:
            end_run(run_id)
        settings_repo.release_instance_lock(conn)
        pipeline_lock.release()


def run_once(client, conn, publish, cfg: dict | None = None) -> dict:
    """One full auto-run: harvest all new jobs, enrich new + stale ones,
    publish alerts. Returns a summary dict (also published by the caller).
    W2.5: cfg (the live config) supplies enrich_workers and
    enrich_interval_days; None behaves as {} (defaults)."""
    cfg = cfg or {}
    # ── Phase 1: harvest ────────────────────────────────────────────────
    existing = job_repo.get_existing_job_ids(conn)
    inserted = 0
    new_titles: list[str] = []
    new_with_salary: list[str] = []
    structure_errors: list[str] = []

    # Saved scrape scope (UI "Save scope for auto-run"). All empty = scrape
    # everything, which is today's behavior and the default.
    scope_kw = (settings_repo.get(conn, "scrape_keyword", "") or "").strip()
    scope_cats = json.loads(settings_repo.get(conn, "scrape_categories", "[]") or "[]")
    scope_skills = json.loads(settings_repo.get(conn, "scrape_skills", "[]") or "[]")
    skill_ids = _resolve_skill_ids(conn, scope_skills)

    for event in harvest(client, existing_ids=existing, keyword=scope_kw,
                         categories=scope_cats or None, skill_ids=skill_ids or None):
        settings_repo.heartbeat_instance_lock(conn)  # W2.6: keep the lock fresh per batch
        if event.type == "harvest_result":
            n, new_items = pipeline_apply.apply_harvest(conn, event)
            inserted += n
            for s, _row_id in new_items:
                t = s.get("title") or ""
                if t:
                    new_titles.append(t)
                if s.get("salary"):
                    new_with_salary.append(t or "?")
        elif event.type == "error":
            structure_errors.append(event.message)
            publish("alert", {"type": "structure", "message": event.message})

    # ── Phase 2: enrich new + stale ────────────────────────────────────
    # Brand-new rows have last_checked NULL, so the "needs enrichment" query
    # is exactly the union of fresh and stale.
    stale = job_repo.get_jobs_needing_enrichment(
        conn,
        max_age_days=int(cfg.get("enrich_interval_days", 7) or 7),  # W2.5
        status_filter=None,
        base_url=getattr(client, "base_url", None),
    )
    jobs_to_enrich = [(r["id"], r["job_url"]) for r in stale if r["job_url"]]

    enriched = closed = errors = 0
    for event in enrich(client, jobs_to_enrich, workers=int(cfg.get("enrich_workers", 3) or 3)):
        settings_repo.heartbeat_instance_lock(conn)  # W2.6: keep the lock fresh per batch
        if event.type == "enrich_result":
            d = event.data
            if "error" in d:
                errors += 1
            elif pipeline_apply.apply_enrich(conn, d):
                enriched += 1
                if d.get("is_closed"):
                    closed += 1
        elif event.type == "error":
            structure_errors.append(event.message)
            publish("alert", {"type": "structure", "message": event.message})

    # ── Auto-apply saved keyword rules ──────────────────────────────────
    auto_hidden = 0
    pos = json.loads(settings_repo.get(conn, "positive_keywords", "[]") or "[]")
    neg = json.loads(settings_repo.get(conn, "negative_keywords", "[]") or "[]")
    if pos or neg:
        from app.server import _apply_keyword_filters  # local import: server imports us
        neg_h, pos_h, _restored = _apply_keyword_filters(conn, pos, neg)
        auto_hidden = neg_h + pos_h
        if auto_hidden:
            publish("alert", {
                "type": "keyword_filter", "count": auto_hidden,
                "message": f"{auto_hidden} job(s) auto-hidden by saved keyword rules",
            })

    # ── Alerts ─────────────────────────────────────────────────────────
    if inserted:
        publish("new_jobs", {"count": inserted, "titles": new_titles[:10]})
    if new_with_salary:
        publish("alert", {
            "type": "salary",
            "count": len(new_with_salary),
            "message": f"{len(new_with_salary)} new job(s) mention salary",
        })
    if closed:
        publish("alert", {
            "type": "jobs_closed", "count": closed,
            "message": f"{closed} job(s) went closed",
        })
    due = job_repo.get_stats(conn)["follow_ups_due"]
    if due:
        publish("alert", {
            "type": "follow_up", "count": due,
            "message": f"{due} follow-up(s) due",
        })

    # W2.8: the run's status is recorded by the caller via record_run_status
    # (a stop is 'stopped', never 'failed'; only a raised error fails).

    return {
        "inserted": inserted,
        "enriched": enriched,
        "closed": closed,
        "errors": errors + len(structure_errors),
        "auto_hidden": auto_hidden,
        "new_titles": new_titles[:10],
        "stopped": client.stopped,
    }


def _resolve_skill_ids(conn, names: list[str]) -> list[int]:
    """Skill names → OJ.ph IDs via the local skill_tags table (the same data
    the UI lists). Unknown names are dropped, not fetched."""
    if not names:
        return []
    lookup = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM skill_tags")}
    return [oid for n in names if (oid := lookup.get(n.lower())) is not None]
