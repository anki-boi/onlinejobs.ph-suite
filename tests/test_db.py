"""
tests/test_db.py — DB schema and repository tests.
"""

import pytest
import sqlite3

from db.connection import SCHEMA
from db.repos import jobs as job_repo
from db.repos import skills as skill_repo


@pytest.fixture
def conn():
    """In-memory DB with schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


class TestSchema:
    def test_jobs_table_exists(self, conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [r[0] for r in rows]
        assert "jobs" in names
        assert "job_history" in names
        assert "skill_tags" in names

    def test_jobs_columns(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        for expected in [
            "id", "job_id", "job_url", "title", "company", "description",
            "salary", "location", "hours_per_week", "work_type",
            "posted_date", "date_updated", "skills", "employer_id",
            "search_keyword", "search_category", "scrape_status", "scrape_reason",
            "status", "date_applied", "notes", "follow_up",
            "date_found", "last_checked",
        ]:
            assert expected in cols, f"Missing column: {expected}"

    def test_unique_job_id(self, conn):
        conn.execute("INSERT INTO jobs (job_id, job_url) VALUES (1, 'http://a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (job_id, job_url) VALUES (1, 'http://b')")

    def test_unique_job_url(self, conn):
        conn.execute("INSERT INTO jobs (job_url) VALUES ('http://a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (job_url) VALUES ('http://a')")


class TestJobRepos:
    def test_upsert_stub_new(self, conn):
        row_id, is_new = job_repo.upsert_stub(
            conn, job_id=100, job_url="http://x.com/job/100",
            title="Test Job", work_type="Full Time",
        )
        assert is_new is True
        assert row_id > 0
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "Test Job"
        assert row["status"] == "New"

    def test_upsert_stub_existing(self, conn):
        job_repo.upsert_stub(conn, job_id=100, job_url="http://x.com/job/100", title="Old")
        row_id, is_new = job_repo.upsert_stub(
            conn, job_id=100, job_url="http://x.com/job/100", title="New",
        )
        assert is_new is False
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "New"

    def test_update_status(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        job_repo.update_status(conn, row_id, "Applied")
        row = job_repo.get_job(conn, row_id)
        assert row["status"] == "Applied"

    def test_update_status_invalid(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        with pytest.raises(ValueError):
            job_repo.update_status(conn, row_id, "Bogus")

    def test_status_history(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        job_repo.update_status(conn, row_id, "Applied")
        job_repo.update_status(conn, row_id, "Interviewing")
        history = job_repo.get_job_history(conn, row_id)
        assert len(history) == 2
        assert history[0]["new_status"] == "Interviewing"
        assert history[1]["new_status"] == "Applied"

    def test_get_stats(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Applied")
        stats = job_repo.get_stats(conn)
        assert stats["total"] == 2
        assert stats["New"] == 1
        assert stats["Applied"] == 1

    def test_get_jobs_filter_by_status(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Applied")

        rows, total = job_repo.get_jobs(conn, status="Applied")
        assert total == 1
        assert rows[0]["title"] == "A"

    def test_get_jobs_exclude_hidden(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Hidden")

        rows, total = job_repo.get_jobs(conn)
        assert total == 1

        rows, total = job_repo.get_jobs(conn, include_hidden=True)
        assert total == 2

    def test_enrich_job(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1")
        job_repo.enrich_job(
            conn, row_id,
            title="Enriched Title",
            company="Some Corp",
            description="Full description here",
            skills=["Python", "Django"],
            employer_id=42,
        )
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "Enriched Title"
        assert row["company"] == "Some Corp"
        assert row["description"] == "Full description here"
        assert row["skills"] == "Python, Django"
        assert row["employer_id"] == 42
        assert row["scrape_status"] == "Open"

    def test_enrich_closed(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1")
        job_repo.enrich_job(conn, row_id, is_closed=True, close_reason="404")
        row = job_repo.get_job(conn, row_id)
        assert row["scrape_status"] == "Closed"
        assert row["scrape_reason"] == "404"


class TestSkillRepos:
    def test_upsert(self, conn):
        rows = [
            {"id": 1, "name": "Python", "parent_id": 10, "slug": "python", "category_path": "Software"},
            {"id": 2, "name": "Django", "parent_id": 11, "slug": "django", "category_path": "Software > Web"},
        ]
        count = skill_repo.upsert_skills(conn, rows)
        assert count == 2
        all_skills = skill_repo.get_all_skills(conn)
        assert len(all_skills) == 2

    def test_search(self, conn):
        skill_repo.upsert_skills(conn, [
            {"id": 1, "name": "Python", "parent_id": None, "slug": "python", "category_path": "Software"},
            {"id": 2, "name": "JavaScript", "parent_id": None, "slug": "javascript", "category_path": "Web"},
        ])
        results = skill_repo.search_skills(conn, "python")
        assert len(results) == 1
        assert results[0]["name"] == "Python"
