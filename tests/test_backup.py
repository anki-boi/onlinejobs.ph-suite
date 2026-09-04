"""
tests/test_backup.py — scripts/backup.py: VACUUM INTO snapshot + keep-N pruning.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backup import make_backup


def _mkdb(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()


def test_make_backup_creates_valid_copy(tmp_path):
    src = tmp_path / "jobs.db"
    _mkdb(src)
    dest_dir = tmp_path / "backups"
    out = make_backup(str(src), str(dest_dir))
    assert out.exists()
    assert out.name.startswith("jobs-")
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    c.close()


def test_make_backup_prunes_to_keep(tmp_path):
    src = tmp_path / "jobs.db"
    _mkdb(src)
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    for i in range(8):
        (dest_dir / f"jobs-2026010{i+1}-000000.db").write_bytes(b"x")
    make_backup(str(src), str(dest_dir))
    left = sorted(dest_dir.glob("jobs-*.db"))
    assert len(left) == 7
    # oldest pruned, newest survive
    assert not (dest_dir / "jobs-20260101-000000.db").exists()
    assert (dest_dir / "jobs-20260108-000000.db").exists()
    assert left[-1].name.startswith("jobs-2026")  # the fresh one survived


def test_make_backup_ignores_foreign_files(tmp_path):
    src = tmp_path / "jobs.db"
    _mkdb(src)
    dest_dir = tmp_path / "backups"
    make_backup(str(src), str(dest_dir))
    (dest_dir / "not-ours.txt").write_text("x")
    make_backup(str(src), str(dest_dir))
    assert (dest_dir / "not-ours.txt").exists()  # pruning never touches foreign files
