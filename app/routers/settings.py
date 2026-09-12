"""app/routers/settings.py — live config view/reload + auto-run schedule.
(W5.1 split; no behaviour change.)"""

from fastapi import HTTPException
from fastapi.routing import APIRouter
from app.schemas import ScheduleUpdate
from app import scheduler
from db.repos import settings as settings_repo
from app.server import get_db

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
    conn = get_db()
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
    conn = get_db()
    if body.enabled is not None:
        settings_repo.set(conn, "auto_run_enabled", "1" if body.enabled else "0")
    if body.interval_hours is not None:
        if not 1 <= body.interval_hours <= 24:
            raise HTTPException(400, "interval_hours must be 1-24")
        settings_repo.set(conn, "auto_run_interval_hours", str(body.interval_hours))
    return get_schedule()
