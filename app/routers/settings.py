"""app/routers/settings.py — live config view/reload + auto-run schedule.
(W5.1 split; no behaviour change.)"""

import json
import sqlite3

from fastapi import HTTPException
from fastapi.routing import APIRouter
from app.schemas import KeywordFilter, ScheduleUpdate, ScrapeScope
from app import scheduler
from app import server as srv
from db.repos import settings as settings_repo
from app.services import keywords as kw

router = APIRouter()


@router.get("/api/config")
def get_config_view():
    """Live config for display — secrets redacted — plus the optional features
    currently off (drives the dismissable UI banner)."""
    from app import config as appconfig
    return {"config": appconfig.redacted(), "features_off": appconfig.features_off()}


@router.post("/api/config/reload")
def reload_config():
    """Re-read config.json + config.local.json; the new values are live
    immediately (LLM tailoring is the motivating case — no restart)."""
    from app import config as appconfig
    before = appconfig.get()
    appconfig.reload()
    after = appconfig.get()
    return {"ok": True, "changed": before != after, "features_off": appconfig.features_off()}


@router.get("/api/schedule")
def get_schedule():
    conn = srv.get_db()
    return {
        "enabled": settings_repo.get(conn, "auto_run_enabled", "1") == "1",
        "interval_hours": int(settings_repo.get(conn, "auto_run_interval_hours",
                                                str(scheduler.DEFAULT_INTERVAL_HOURS))
                              or scheduler.DEFAULT_INTERVAL_HOURS),
        "last_run": settings_repo.get(conn, "last_run", ""),
        "last_error": settings_repo.get(conn, "last_error", ""),
        "last_status": settings_repo.get(conn, "last_status", ""),
        "next_run": settings_repo.get(conn, "next_run", ""),
        "running": scheduler.is_running(),
        # W2.6: cross-process lock holder (for /health-style status + the UI)
        "instance_lock": settings_repo.instance_lock_holder(conn),
    }


@router.post("/api/schedule")
def set_schedule(body: ScheduleUpdate):
    conn = srv.get_db()
    if body.enabled is not None:
        settings_repo.set(conn, "auto_run_enabled", "1" if body.enabled else "0")
    if body.interval_hours is not None:
        if not 1 <= body.interval_hours <= 24:
            raise HTTPException(400, "interval_hours must be 1-24")
        settings_repo.set(conn, "auto_run_interval_hours", str(body.interval_hours))
    return get_schedule()


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
