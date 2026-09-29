"""
tests/test_migrations_v8.py — v8: the login banner stops being an employer, and
the ~90% of rows with no employer_id get a company-based repost pass.
"""

import sqlite3

import pytest

import db.connection as dbconn
from db.migrations.v8_company_cleanup import step as v8
from db.repos import jobs as job_repo


@pytest.fixture
def conn(tmp_path, monkeypatch):
    dbpath = tmp_path / "v8.db"
    monkeypatch.setattr(dbconn, "DB_PATH", str(dbpath))
    c = dbconn.init_db(dbconn.get_conn())
    yield c
    c.close()


def _job(conn, job_id, title, company, employer_id=None):
    rid, _ = job_repo.upsert_stub(conn, job_id=job_id, job_url=f"http://x/{job_id}",
                                  title=title, company=company)
    job_repo.enrich_job(conn, rid, title=title, company=company, employer_id=employer_id)
    return rid


def test_banner_rows_are_emptied(conn):
    """The literal sentence goes in as a company (the old bare-<h3> fallback);
    v8 must leave the column NULL, not 'fixed' to something else."""
    rid = _job(conn, 1, "VA", "Please login or register as jobseeker to apply for this job.")
    v8(conn)
    assert conn.execute("SELECT company, norm_company FROM jobs WHERE id=?", (rid,)).fetchone()[0] is None
    assert conn.execute("SELECT norm_company FROM jobs WHERE id=?", (rid,)).fetchone()[0] is None


def test_parser_can_no_longer_write_the_banner_back(conn):
    """New scrapes are guarded at the repo boundary, so a re-run doesn't undo v8."""
    rid, _ = job_repo.upsert_stub(conn, job_id=2, job_url="http://x/2", title="VA",
                                  company="Please login or register as jobseeker to apply for this job.")
    assert conn.execute("SELECT company FROM jobs WHERE id=?", (rid,)).fetchone()[0] is None


def test_real_company_keeps_its_fold(conn):
    rid = _job(conn, 3, "VA", "Acme, Inc.")
    v8(conn)
    assert conn.execute("SELECT norm_company FROM jobs WHERE id=?", (rid,)).fetchone()[0] == "acmeinc"


def test_company_based_repost_backfill(conn):
    """B4: employer_id is NULL on 1,115 of 1,232 rows, so the old employer_id-only
    pass found almost nothing. The backfill uses title + company."""
    a = _job(conn, 11, "Executive Assistant", "Project OPM")
    b = _job(conn, 12, "executive assistant", "project opm")
    conn.execute("UPDATE jobs SET repost_of = NULL")   # simulate pre-v8 state
    v8(conn)
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] == a


def test_v8_is_idempotent(conn):
    _job(conn, 21, "VA", "Acme")
    _job(conn, 22, "va", "acme")
    v8(conn)
    first = conn.execute("SELECT id, repost_of FROM jobs ORDER BY id").fetchall()
    v8(conn)
    assert conn.execute("SELECT id, repost_of FROM jobs ORDER BY id").fetchall() == first


@pytest.mark.parametrize("cols", [
    "id INTEGER PRIMARY KEY, title TEXT, company TEXT",                       # pre-W4
    "id INTEGER PRIMARY KEY, title TEXT, company TEXT, norm_title TEXT",       # pre-W4.3
])
def test_v8_survives_a_legacy_schema(tmp_path, cols):
    """A DB from before W4.3 lacks repost_of/employer_id (and maybe company);
    v8 must clean what it can and not crash on the columns it doesn't have."""
    legacy = sqlite3.connect(str(tmp_path / "legacy.db"))
    legacy.execute(f"CREATE TABLE jobs ({cols})")
    legacy.execute("INSERT INTO jobs (id, title) VALUES (1, 'VA')")
    legacy.commit()
    v8(legacy)
    assert "norm_company" in {r[1] for r in legacy.execute("PRAGMA table_info(jobs)")}
