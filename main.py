#!/usr/bin/env python3
"""
main.py — Entry point for the Job Hunter dashboard.

Usage:
    python main.py              # Start server on port 8371
    python main.py --port 8080  # Custom port
    python main.py --host 0.0.0.0

Ctrl-C / shutdown (W2.9): the first Ctrl-C sends a stop signal to the
in-flight pipeline run (if any), then uvicorn stops accepting connections
and waits for the run's stream to end — up to 10 seconds (forced after);
a second Ctrl-C during shutdown exits immediately. If the in-flight run was
the auto-run thread (not an open stream), main() waits up to 10 s more for it
to finish and record its status before exiting. The process ends with a
summary line: last run time + status (completed / stopped with partial
results / failed). jobs.db is closed by the app's lifespan shutdown.
"""

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.connection import init_db
from scraper.skills import fetch_skills, skills_to_db_rows
from db.repos.skills import upsert_skills
from scraper.client import OJClient


def load_config() -> dict:
    """The live merged config (W1.4) — config.json + gitignored overlay, owned
    by app/config.py; the old copy ignored config.local.json."""
    from app import config as appconfig
    return appconfig.get()


def initial_skills_refresh(cfg: dict) -> None:
    """On first run, fetch the full skill taxonomy from the API."""
    conn = init_db()
    existing = conn.execute("SELECT COUNT(*) FROM skill_tags").fetchone()[0]
    if existing > 100:
        print(f"  [ok] skill_tags already populated ({existing} skills)")
        return

    print("  [..] Fetching skills from API...")
    client = OJClient(
        base_url=cfg.get("base_url", "https://www.onlinejobs.ph"),
        api_url=cfg.get("api_url", "https://api.onlinejobs.ph"),
        delay=cfg.get("request_delay", 1.0),
        max_retries=cfg.get("max_retries", 3),
    )
    try:
        skills = fetch_skills(client, keyword="")
        rows = skills_to_db_rows(skills)
        count = upsert_skills(conn, rows)
        print(f"  [ok] Loaded {count} skills")
    except Exception as exc:
        print(f"  [!] Could not refresh skills: {exc}")
        print("  (Will retry on first /api/skills/refresh call)")


def _setup_logging() -> None:
    """Rotate log files so they never grow unbounded (5 MB x 3 backups)."""
    logger = logging.getLogger()
    # Remove default handlers
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.handlers.RotatingFileHandler(
        PROJECT_ROOT / "job_hunter.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    # Also stream INFO+ to stderr for interactive runs
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh.addFilter(lambda r: r.levelno >= logging.INFO)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)


def make_server(host: str, port: int):
    """uvicorn server with the W2.9 graceful-shutdown hook installed:
    on SIGINT/SIGTERM it first flips the in-flight run's stop token (via
    scheduler.stop_run) and then lets uvicorn drain (≤10 s force cap)."""
    import uvicorn
    from app import scheduler

    server = uvicorn.Server(uvicorn.Config(
        "app.server:app",
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=10,
    ))
    orig_handle_exit = server.handle_exit

    def handle_exit(sig, frame):
        if scheduler.stop_run():
            print("\n  [..] Stop signal sent to the running pipeline; "
                  "waiting for it to end (max 10 s)...")
        else:
            print("\n  [..] Shutting down...")
        orig_handle_exit(sig, frame)

    server.handle_exit = handle_exit
    return server


def shutdown_summary() -> None:
    """W2.9: the final line after uvicorn drains — where the last run ended."""
    import db.connection as dbconn
    from db.repos import settings as settings_repo

    conn = dbconn.get_conn()
    last_run = settings_repo.get(conn, "last_run", "")
    last_status = settings_repo.get(conn, "last_status", "")
    last_error = settings_repo.get(conn, "last_error", "")
    conn.close()

    line = f"  Job Hunter stopped. Last run: {last_run or 'never'}"
    if last_status == "failed":
        line += f" (failed: {last_error})"
    elif last_status == "stopped":
        line += " (stopped - partial results kept in jobs.db)"
    print(line)


def main():
    _setup_logging()
    parser = argparse.ArgumentParser(description="Job Hunter — OnlineJobs.ph tracker")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8371, help="Port (default: 8371)")
    parser.add_argument("--skip-skills", action="store_true", help="Skip initial skills refresh")
    args = parser.parse_args()

    print(f"  Job Hunter starting on http://{args.host}:{args.port}")

    # Init DB
    cfg = load_config()
    print("  [..] Initialising database...")
    init_db()

    # Initial skills refresh
    if not args.skip_skills:
        initial_skills_refresh(cfg)

    # Run server
    from app import scheduler
    scheduler.start()  # daemon thread: auto-run harvest+enrich on the configured interval
    server = make_server(args.host, args.port)
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # second Ctrl-C during shutdown: exit now
    if not server.force_exit:
        # W2.9: the auto-run thread is a bare daemon thread (not an open
        # connection), so uvicorn's drain won't wait for it. Give it up to
        # 10 s to notice the stop token, finish, and record its status before
        # the summary prints. The pipeline lock means at most one run is in
        # flight, so this never stacks on the SSE drain cap.
        scheduler.wait_runs(10)
    shutdown_summary()


if __name__ == "__main__":
    main()
