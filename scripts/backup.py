"""
scripts/backup.py -- point-in-time backup of jobs.db via SQLite VACUUM INTO.

Usage (direct):       python scripts/backup.py [db_path] [backup_dir] [keep]
Usage (scheduled):    schtasks /run /tn "JobHunter-Backup"   (daily 03:00)

VACUUM INTO produces a fresh, self-contained, zero-WAL db file -- the safest
no-extra-dependency snapshot SQLite offers. Keeps the newest `keep` copies.

Retention defaults:
  - CLI arg overrides -> config.json backup_retention_days -> hardcoded 7.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Defaults for the scheduled-task entrypoint (python scripts/backup.py).
# DB_PATH honors JOBS_DB_PATH / config.json like the rest of the app so the
# backup always targets whatever DB the server is actually using.


def _default_db_path() -> str:
    import os

    env = os.environ.get("JOBS_DB_PATH")
    if env:
        return env if os.path.isabs(env) else str(ROOT / env)
    try:
        import db.connection as _dc

        return str(_dc.get_config().get("db_path", "jobs.db"))
    except Exception:
        return str(ROOT / "jobs.db")


DB_PATH = _default_db_path()
BACKUP_DIR = ROOT / "backups"


def _load_keep() -> int:
    """Retention days from config.json, falling back to CLI arg then hardcoded default."""
    # CLI override
    if len(sys.argv) > 3 and sys.argv[3].isdigit():
        return int(sys.argv[3])
    # Config overlay
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
        v = cfg.get("backup_retention_days")
        if v and isinstance(v, (int, float)) and v > 0:
            return int(v)
    except Exception:
        pass
    # Hardcoded default
    return 7


def make_backup(db_path: str, dest_dir: str, keep: int | None = None) -> Path:
    """Snapshot db_path into dest_dir; prune to the newest `keep` copies.
    Returns the new file's path."""
    if keep is None:
        keep = _load_keep()

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest / f"jobs-{stamp}.db"
    out.unlink(missing_ok=True)  # VACUUM INTO refuses an existing file (same-second re-run)
    c = sqlite3.connect(db_path)
    try:
        c.execute("VACUUM INTO ?", (str(out),))
    finally:
        c.close()
    # Prune oldest beyond `keep` -- only files this script names, never foreign ones
    mine = sorted(dest.glob("jobs-*.db"))
    for old in mine[: max(0, len(mine) - keep)]:  # oldest first; foreign files untouched
        old.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    db_path = args[0] if len(args) > 0 else DB_PATH
    dest_dir = args[1] if len(args) > 1 else str(BACKUP_DIR)
    keep = int(args[2]) if len(args) > 2 else None
    p = make_backup(str(db_path), str(dest_dir), keep)
    n = len(list(Path(dest_dir).glob("jobs-*.db")))
    print(f"backup ok: {p.name}  ({n} kept)")
