"""
tests/test_w43.py — W4.3: materialized ATS scores; min_ats is a SQL-level
filter so `total` is a truthful global count (was a per-page Python filter
that returned a page-sized total).
"""

import sqlite3

import db.connection as dbconn
import db.migrate as dbmigrate
import resumes.schema as rschema
from app import config as appconfig
from db.repos import jobs as job_repo


def _fresh(tmp_path, monkeypatch):
    dbpath = tmp_path / "w43.db"
    monkeypatch.setattr(dbconn, "DB_PATH", dbpath)
    monkeypatch.setattr(appconfig, "_live", {})
    monkeypatch.setattr(appconfig, "_read_disk", lambda: {})
    dbconn.init_db()
    conn = sqlite3.connect(str(dbpath))
    conn.row_factory = sqlite3.Row
    return conn


def _fake_scorer(masters, job):
    """Deterministic: score = the number in the title ("Job 12" → 12)."""
    n = int(job["title"].rsplit(" ", 1)[-1])
    return ("p1", {"total": float(n)})


def test_v6_migration_idempotent(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    assert dbconn.SCHEMA_VERSION == 7
    dbmigrate.MIGRATIONS[6](conn)
    dbmigrate.MIGRATIONS[6](conn)  # runs twice safely
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ats_scores" in tables


def test_min_ats_global_total(client, monkeypatch):
    monkeypatch.setattr(rschema, "best_profile_for_job", _fake_scorer)
    from db.connection import get_conn
    conn = get_conn()
    for n in range(1, 121):
        job_repo.upsert_stub(conn, job_id=n, job_url=f"https://x/{n}", title=f"Job {n}")

    # N=60..120 score >= 60 → 61 jobs; total must be that, not a page-sized count
    res = client.get("/api/jobs", params={"min_ats": 60, "per_page": 50})
    data = res.json()
    assert data["total"] == 61
    assert len(data["jobs"]) == 50
    res2 = client.get("/api/jobs", params={"min_ats": 60, "per_page": 50, "page": 2})
    assert len(res2.json()["jobs"]) == 11
    # nothing below the threshold leaks in
    assert all(int(j["title"].rsplit(" ", 1)[-1]) >= 60 for j in data["jobs"])


def test_min_ats_invalidates_on_job_change(client, monkeypatch):
    monkeypatch.setattr(rschema, "best_profile_for_job", _fake_scorer)
    from db.connection import get_conn
    conn = get_conn()
    job_repo.upsert_stub(conn, job_id=1, job_url="https://x/1", title="Job 1")
    job_repo.upsert_stub(conn, job_id=2, job_url="https://x/2", title="Job 100")
    assert client.get("/api/jobs", params={"min_ats": 50}).json()["total"] == 1

    # in-place change bumps the jobs version → cache must re-score
    job_repo.enrich_job(conn, 1, title="Job 120")
    res = client.get("/api/jobs", params={"min_ats": 50})
    assert res.json()["total"] == 2
    row = conn.execute("SELECT total FROM ats_scores WHERE job_id=1").fetchone()
    assert row["total"] == 120.0


def test_no_rescore_churn(client, monkeypatch):
    monkeypatch.setattr(rschema, "best_profile_for_job", _fake_scorer)
    calls = {"n": 0}
    real = _fake_scorer
    def counting(masters, job):
        calls["n"] += 1
        return real(masters, job)
    monkeypatch.setattr(rschema, "best_profile_for_job", counting)
    from db.connection import get_conn
    conn = get_conn()
    job_repo.upsert_stub(conn, job_id=1, job_url="https://x/1", title="Job 5")

    client.get("/api/jobs")
    first = calls["n"]
    assert first == 1  # scored once
    client.get("/api/jobs")
    assert calls["n"] == first  # key unchanged → no re-score
