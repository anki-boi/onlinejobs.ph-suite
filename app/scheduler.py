"""
app/scheduler.py — auto-run: harvest + enrich on an interval in a background
thread, publishing alerts (new jobs, closures, salary, follow-ups, structure
changes) via the app.events hub so any open dashboard tab can raise a
desktop notification.

State lives in the app_settings table (survives restarts):
  auto_run_enabled            "1"/"0"       (default on — user chose every 4h)
  auto_run_interval_hours     "1".."24"     (default 4)
  last_run / last_error       for the UI
  next_run                    epoch seconds; the tick fires when now >= it
"""

import json
import logging
import threading
import time

import db.connection as dbconn
from app import events
from app import pipeline_apply
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from scraper.pipeline import enrich, harvest

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 4

# Manual pipeline endpoints take this too, so an auto-run and a hand-pressed
# Run never fire two scrapes at the site at once.
pipeline_lock = threading.Lock()


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
    try:
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
        summary = run_once(client_factory(), conn, events.publish, cfg)
        events.publish("schedule_done", summary)
    except Exception as exc:
        log.warning(f"auto-run failed: {exc}")
        settings_repo.set(conn, "last_error", str(exc))
        events.publish("alert", {"type": "error", "message": f"auto-run failed: {exc}"})
    finally:
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

    return {
        "inserted": inserted,
        "enriched": enriched,
        "closed": closed,
        "errors": errors + len(structure_errors),
        "auto_hidden": auto_hidden,
        "new_titles": new_titles[:10],
    }


def _resolve_skill_ids(conn, names: list[str]) -> list[int]:
    """Skill names → OJ.ph IDs via the local skill_tags table (the same data
    the UI lists). Unknown names are dropped, not fetched."""
    if not names:
        return []
    lookup = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM skill_tags")}
    return [oid for n in names if (oid := lookup.get(n.lower())) is not None]
