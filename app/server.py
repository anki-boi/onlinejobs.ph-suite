"""
app/server.py — FastAPI application: composition root (W5.1).

Owns the shared state (client, config, masters paths, health probes) and the
health/index routes; every API route group lives in app/routers/*.py and is
mounted here at the bottom of the module (so the routers can import the
shared names from this module without a circular-import crash).

Run:  uvicorn app.server:app --reload
"""

import logging
import os
import sqlite3

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.sse import sse as _sse  # noqa: F401 (re-exported for routers/tests)
from app import deps
import db.connection as dbconn
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo
from resumes import schema as resume_schema  # noqa: F401 (re-exported for routers/tests)
from resumes import render as resume_render  # noqa: F401 (re-exported for routers/tests)
from resumes import ats as resume_ats_mod  # noqa: F401 (re-exported for routers/tests)
from resumes.schema import validate  # noqa: F401 (re-exported for routers/tests)
from resumes.tailor import LLMClient, job_brief, tailor  # noqa: F401 (re-exported for routers/tests)
from scraper.client import DEFAULT_BASE_URL, OJClient
from scraper.pipeline import enrich, harvest  # noqa: F401 (re-exported for routers/tests)
from scraper.skills import fetch_skills  # noqa: F401 (re-exported for routers/tests)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App setup ───────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    # W2.1: release every shared SQLite handle on shutdown (SIGINT/SIGTERM via uvicorn).
    yield
    deps.close_all()


app = FastAPI(title="Job Hunter", lifespan=_lifespan)

STATIC_DIR = dbconn.BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class _LiveCfg:
    """W1.4: every read goes to the live config state, so a
    POST /api/config/reload is picked up immediately (no restart)."""

    def get(self, key, default=None):
        from app import config as appconfig
        return appconfig.get().get(key, default)


_cfg = _LiveCfg()

_client: OJClient | None = None
RESUME_PATH = dbconn.BASE_DIR / "resumes" / "master.json"
MASTERS_PATH = dbconn.BASE_DIR / "resumes" / "masters.json"
BUILT_DIR = dbconn.BASE_DIR / "resumes" / "built"
BASE_RESUMES = dbconn.BASE_DIR / "resumes"


def _masters() -> dict:
    return resume_schema.load_masters(MASTERS_PATH)


def _ats_cache_key(conn) -> str:
    """Invalidation key for the materialized ATS cache (W4.3): any in-place
    score-field change bumps the jobs version counter; masters.json mtime
    covers resume-side changes."""
    mtime = MASTERS_PATH.stat().st_mtime if MASTERS_PATH.exists() else 0
    return f"{job_repo.get_jobs_version(conn)}:{mtime}"


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


def _claim_run_or_busy(conn) -> tuple[bool, str]:
    """W2.6: claim the cross-process pipeline lock for this run.
    (ok, message) — False with a user-facing message when another live
    instance holds it."""
    holder = settings_repo.instance_lock_holder(conn)
    if holder and not holder["stale"] and holder["pid"] != os.getpid():
        return False, f"another instance is running the pipeline (pid {holder['pid']})"
    if settings_repo.acquire_instance_lock(conn):
        return True, ""
    return False, "another instance just claimed the pipeline"

# (W2.1) per-request connections come from app.deps.get_db — one shared
# connection per (thread, db path), schema ensured once per process per path,
# all closed on shutdown. Throwaway connections: dbconn.get_conn().
get_db = deps.get_db


def _job_dict_for_resume(row) -> dict:
    """Map a jobs row to the resume/ATS job dict (shared by the ATS cache
    build and every resume route)."""
    return {
        "title": row["title"],
        "employer": row["company"],
        "skills": row["skills"],
        "keywords": row["search_keyword"],
        "salary": row["salary"],
        "description": row["description"],
    }


# Keyword auto-hide rules live in app/services (shared with the auto-run
# scheduler). Re-exported here so `app.server._apply_keyword_filters` /
# `_keyword_regexes` keep working for callers written before the W5.1 split.
from app.services import keywords as _kw  # noqa: E402
apply_keyword_filters = _kw.apply_keyword_filters
_apply_keyword_filters = _kw.apply_keyword_filters
_keyword_regexes = _kw._keyword_regexes

# ── Health check ────────────────────────────────────────────────────────────

def _probe_db() -> str:
    """W1.5: 'ok' or 'locked'. Short (1 s) busy timeout so a locked DB
    reports locked instead of hanging the probe for the usual 30 s."""
    conn = None
    try:
        conn = sqlite3.connect(dbconn._resolve_db_path(), timeout=1)
        conn.execute("SELECT 1")
        return "ok"
    except sqlite3.OperationalError:
        return "locked"
    finally:
        if conn is not None:
            conn.close()


def _probe_site(url: str | None = None) -> str:
    """W1.5: 'ok' or 'unreachable'. Any HTTP answer counts as reachable."""
    try:
        requests.get(url or DEFAULT_BASE_URL + "/", timeout=5)
        return "ok"
    except requests.RequestException:
        return "unreachable"


@app.get("/health")
async def health():
    """W1.5: real health — 200 ok; 200 degraded if the DB is locked (app
    still serves cached data); 503 degraded if the site is unreachable
    (scraping is down). Probes run in worker threads, never the event loop."""
    import asyncio

    db = await asyncio.to_thread(_probe_db)
    site = await asyncio.to_thread(_probe_site)
    body = {"status": "ok", "db": db, "site": site, "pid": os.getpid()}
    if site != "ok":
        body["status"] = "degraded"
        return JSONResponse(body, status_code=503)
    if db != "ok":
        body["status"] = "degraded"
    return body


# ── HTML ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── Route groups (W5.1) ─────────────────────────────────────────────────────
# Imported last: the routers import the shared names from this module at the
# top of the file, so every one of those names must already exist by now.
from app.routers import events as _r_events      # noqa: E402
from app.routers import jobs as _r_jobs          # noqa: E402
from app.routers import pipeline as _r_pipeline  # noqa: E402
from app.routers import resume as _r_resume      # noqa: E402
from app.routers import settings as _r_settings  # noqa: E402
from app.routers import skills as _r_skills      # noqa: E402

app.include_router(_r_jobs.router)
app.include_router(_r_pipeline.router)
app.include_router(_r_resume.router)
app.include_router(_r_settings.router)
app.include_router(_r_events.router)
app.include_router(_r_skills.router)

# Re-exported for callers/tests written before the W5.1 split.
events_stream = _r_events.events_stream
