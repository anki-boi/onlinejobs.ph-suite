#!/usr/bin/env python3
"""
main.py — Entry point for the Job Hunter dashboard.

Usage:
    python main.py              # Start server on port 8371
    python main.py --port 8080  # Custom port
    python main.py --host 0.0.0.0
"""

import argparse
import json
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
    cfg_path = PROJECT_ROOT / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text())
    return {}


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


def main():
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
    import uvicorn
    from app import scheduler
    scheduler.start()  # daemon thread: auto-run harvest+enrich on the configured interval
    uvicorn.run(
        "app.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
