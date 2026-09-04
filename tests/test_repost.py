"""
tests/test_repost.py — repost detection: same normalized title + same
employer, different row → mark repost_of. Badge-only: nothing is deleted,
and a row is never its own repost.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import db.connection as dbconn
from db.repos import jobs as job_repo


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "repost.db"))
    c = dbconn.init_db(dbconn.get_conn())
    yield c
    c.close()


def _add(conn, job_id, url, title, employer):
    row_id, _ = job_repo.upsert_stub(
        conn, job_id=job_id, job_url=url, title=title, company="X")
    job_repo.enrich_job(conn, row_id, title=title, employer_id=employer)
    return row_id


def test_same_title_same_employer_is_repost(conn):
    a = _add(conn, 1, "http://x/1", "PHP Developer", 10)
    b = _add(conn, 2, "http://x/2", "php  DEVELOPER", 10)  # whitespace/case noise
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] == a
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (a,)).fetchone()[0] is None


def test_same_title_different_employer_not_repost(conn):
    _add(conn, 1, "http://x/1", "PHP Developer", 10)
    b = _add(conn, 2, "http://x/2", "PHP Developer", 20)
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] is None


def test_no_employer_no_repost(conn):
    _add(conn, 1, "http://x/1", "PHP Developer", 10)
    b = _add(conn, 2, "http://x/2", "PHP Developer", None)
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] is None


def test_second_repost_points_at_newest_original(conn):
    a = _add(conn, 1, "http://x/1", "PHP Developer", 10)
    b = _add(conn, 2, "http://x/2", "PHP Developer", 10)
    # enrich a = the repost row again → it must not point at itself
    job_repo.enrich_job(conn, b, title="php developer", employer_id=10)
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] == a


def test_salary_parsed_into_min_max(conn):
    row_id, _ = job_repo.upsert_stub(conn, job_id=5, job_url="http://x/5", title="T")
    job_repo.enrich_job(conn, row_id, salary="40000-50000php")
    r = conn.execute("SELECT salary_min, salary_max FROM jobs WHERE id=?", (row_id,)).fetchone()
    assert (r[0], r[1]) == (40000.0, 50000.0)


def test_salary_unparsed_leaves_nulls(conn):
    row_id, _ = job_repo.upsert_stub(conn, job_id=6, job_url="http://x/6", title="T")
    job_repo.enrich_job(conn, row_id, salary="DOE")
    r = conn.execute("SELECT salary_min, salary_max FROM jobs WHERE id=?", (row_id,)).fetchone()
    assert (r[0], r[1]) == (None, None)
