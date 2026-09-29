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


def test_no_employer_same_company_is_a_repost(conn):
    """B4 changed this expectation: with no employer_id, the company text is the
    key, and both rows here carry company="X" — so they are a repost pair."""
    _add(conn, 1, "http://x/1", "PHP Developer", 10)
    b = _add(conn, 2, "http://x/2", "PHP Developer", None)
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0]


def test_no_employer_and_no_company_is_not_a_repost(conn):
    row_id, _ = job_repo.upsert_stub(conn, job_id=31, job_url="http://x/31", title="Lonely Job")
    job_repo.enrich_job(conn, row_id, title="Lonely Job")
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (row_id,)).fetchone()[0] is None


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


def _add_company(conn, job_id, url, title, company):
    row_id, _ = job_repo.upsert_stub(conn, job_id=job_id, job_url=url,
                                     title=title, company=company)
    job_repo.enrich_job(conn, row_id, title=title, company=company)
    return row_id


def test_company_fallback_detects_repost_without_employer_id(conn):
    """B4: employer_id is NULL on 1,115 of 1,232 rows (the employer logo is behind
    a jobseeker login), so title+company is the only key that exists for most of
    the DB. Reposts were going largely undetected."""
    a = _add_company(conn, 1, "http://x/1", "PHP Developer", "Project OPM")
    b = _add_company(conn, 2, "http://x/2", "php developer", "project opm")
    assert conn.execute("SELECT employer_id FROM jobs WHERE id=?", (b,)).fetchone()[0] is None
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] == a


def test_same_title_different_company_is_not_a_repost(conn):
    _add_company(conn, 1, "http://x/1", "PHP Developer", "Project OPM")
    b = _add_company(conn, 2, "http://x/2", "PHP Developer", "Star Lab")
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] is None


def test_login_banner_company_is_not_an_employer(conn):
    """L1: the scraped page chrome ("Please login or register…") must not turn
    every job into a repost of an unrelated job with the same title."""
    a = _add_company(conn, 1, "http://x/1", "VA", "Please login or register as jobseeker to apply for this job.")
    b = _add_company(conn, 2, "http://x/2", "VA", "Please login or register as jobseeker to apply for this job.")
    # Both rows carry the same banner, so they do group — but the banner is
    # nulled by migration v8, after which neither is a repost.
    from db.migrations.v8_company_cleanup import step as v8
    v8(conn)
    assert conn.execute("SELECT company FROM jobs WHERE id=?", (a,)).fetchone()[0] is None
    assert conn.execute("SELECT repost_of FROM jobs WHERE id=?", (b,)).fetchone()[0] is None


def test_new_job_gets_an_ats_cache_key_immediately(conn):
    """B3: the INSERT path never bumped jobs_version, so a freshly harvested job
    had no ats_scores row until some unrelated UPDATE bumped it — and under
    "ATS >= 50" (EXISTS on ats_scores) brand-new jobs were invisible."""
    v0 = job_repo.get_jobs_version(conn)
    job_repo.upsert_stub(conn, job_id=77, job_url="http://x/77", title="New harvest")
    assert job_repo.get_jobs_version(conn) == v0 + 1
