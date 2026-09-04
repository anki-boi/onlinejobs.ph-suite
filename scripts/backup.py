"""
scripts/backup.py — point-in-time backup of jobs.db via SQLite VACUUM INTO.

Usage (direct):       python scripts/backup.py
Usage (scheduled):    schtasks /run /tn "JobHunter-Backup"   (daily 03:00)

VACUUM INTO produces a fresh, self-contained, zero-WAL db file — the safest
no-extra-dependency snapshot SQLite offers. Keeps the newest `KEEP` copies.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "jobs.db"
BACKUP_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "backups"
KEEP = 7


def make_backup(db_path: str, dest_dir: str, keep: int = KEEP) -> Path:
    """Snapshot db_path into dest_dir; prune to the newest `keep` copies.
    Returns the new file's path."""
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
    # Prune oldest beyond `keep` — only files this script names, never foreign ones
    mine = sorted(dest.glob("jobs-*.db"))
    for old in mine[: max(0, len(mine) - keep)]:  # oldest first; foreign files untouched
        old.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    p = make_backup(str(DB_PATH), str(BACKUP_DIR))
    n = len(list(BACKUP_DIR.glob("jobs-*.db")))
    print(f"backup ok: {p.name}  ({n} kept)")
