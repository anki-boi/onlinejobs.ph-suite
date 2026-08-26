"""
tests/test_api.py — FastAPI endpoint tests using the TestClient.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from app.server import app
from db.connection import init_db, BASE_DIR, SCHEMA
import sqlite3


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a test client with a temp DB."""
    db_path = str(tmp_path / "test.db")

    # Monkeypatch the DB path at module level
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", db_path)

    # Create tables on the temp DB
    from db.connection import init_db as _init_db
    _init_db()

    # Reset the server's client
    from app import server as srv
    monkeypatch.setattr(srv, "_client", None)

    with TestClient(app) as c:
        yield c


class TestStats:
    def test_empty(self, client):
        res = client.get("/api/stats")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0


class TestJobsList:
    def test_empty(self, client):
        res = client.get("/api/jobs")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["jobs"] == []

    def test_with_jobs(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B")
        conn.close()

        res = client.get("/api/jobs")
        data = res.json()
        assert data["total"] == 2
        assert len(data["jobs"]) == 2

    def test_filter_by_status(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B")
        job_repo.update_status(conn, 1, "Applied")
        conn.close()

        res = client.get("/api/jobs?status=Applied")
        data = res.json()
        assert data["total"] == 1
        assert data["jobs"][0]["title"] == "Job A"

    def test_skills_or_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A",
                             skills=["Video Editing", "Audio Editing"])
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B",
                             skills=["Quickbooks"])
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="Job C",
                             skills=["Marketing"])
        conn.close()

        res = client.get("/api/jobs?skills=Quickbooks,Audio%20Editing&per_page=99999")
        data = res.json()
        # OR across the whole table, not just one page
        assert data["total"] == 2
        assert {j["job_id"] for j in data["jobs"]} == {1, 2}

    def test_sort_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="C job")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="A job")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="B job")
        conn.close()

        data = client.get("/api/jobs?sort=title&order=asc").json()
        assert [j["title"] for j in data["jobs"]] == ["A job", "B job", "C job"]

        data = client.get("/api/jobs?sort=title&order=desc").json()
        assert [j["title"] for j in data["jobs"]] == ["C job", "B job", "A job"]

        # unknown sort falls back to default ordering, no error
        data = client.get("/api/jobs?sort=nonexistent").json()
        assert data["total"] == 3

    def test_has_salary_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="A", salary="$500/month")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="B", salary="TBD")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="C")  # no salary
        conn.close()

        data = client.get("/api/jobs?has_salary=1").json()
        assert data["total"] == 1
        assert data["jobs"][0]["job_id"] == 1

        # default = all
        data = client.get("/api/jobs").json()
        assert data["total"] == 3


class TestKeywordApply:
    def _seed(self):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1",
                             title="Bookkeeper", company="Xero Co")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2",
                             title="Video Editor", company="Globex")
        conn.execute("UPDATE jobs SET description=? WHERE job_id=1",
                     ("We love Xero and Quickbooks.",))
        conn.execute("UPDATE jobs SET description=? WHERE job_id=2",
                     ("Cut videos all day.",))
        conn.commit()
        conn.close()

    def test_no_keywords_noop(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.status_code == 200
        assert res.json()["total_hidden"] == 0

    def test_negative_hides_matching(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.status_code == 200
        assert res.json()["hidden_by_negative"] == 1

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "Hidden"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "New"
        hist = conn.execute(
            "SELECT old_status FROM job_history WHERE new_status='Hidden' AND job_id=1"
        ).fetchall()
        assert len(hist) == 1 and hist[0][0] == "New"
        conn.close()

    def test_positive_hides_nonmatching_new(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        assert res.status_code == 200
        assert res.json()["hidden_by_positive"] == 1  # only the video editor

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "New"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "Hidden"
        conn.close()

    def test_unenriched_untouched(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=9, job_url="http://test.com/9", title="Stub Only")
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        assert res.status_code == 200
        assert res.json()["total_hidden"] == 0  # no description → untouched

    def test_reapply_after_removing_keywords_restores(self, client):
        """The user's exact scenario: filter hides a job, user changes their
        mind, removes the keyword, re-applies → job comes back."""
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.json()["hidden_by_negative"] == 1

        # Remove the keyword and re-apply (no keywords at all now)
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.json()["restored"] == 1
        assert res.json()["total_hidden"] == 0

        from db.connection import get_conn
        conn = get_conn()
        row = conn.execute("SELECT status, filter_hidden FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "New" and row["filter_hidden"] == 0
        conn.close()

    def test_user_hidden_not_auto_restored(self, client):
        """Jobs the user hid by hand (filter_hidden=0) stay hidden."""
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        self._seed()
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=2").fetchone()[0]
        job_repo.update_status(conn, row_id, "Hidden")  # manual hide
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        # job 2 has no 'xero' — but it's manually hidden, so restore must not touch it
        assert res.json()["restored"] == 0

        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "Hidden"
        conn.close()

    def test_manual_restate_not_clobbered(self, client):
        """Filter hides a job → user sets it to 'Applied' by hand → a later
        apply with removed keywords must NOT revert it to 'New'."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})

        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=1").fetchone()[0]
        job_repo.update_status(conn, row_id, "Applied")
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.json()["restored"] == 0

        conn = get_conn()
        row = conn.execute("SELECT status, filter_hidden FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "Applied" and row["filter_hidden"] == 0
        conn.close()

    def test_rehide_saves_current_status(self, client):
        """A job the user re-statused can be hidden again by a new filter run;
        its CURRENT status is what gets saved for a future restore."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})

        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=1").fetchone()[0]
        job_repo.update_status(conn, row_id, "Applied")
        conn.close()

        # New filter run: negative 'xero' still matches → hides again, saving 'Applied'
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.json()["hidden_by_negative"] == 1

        conn = get_conn()
        row = conn.execute("SELECT status, pre_filter_status FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "Hidden" and row["pre_filter_status"] == "Applied"
        conn.close()

    def test_restore_all_flag(self, client):
        """The 'Restore' button: brings back everything filter-hidden even if
        keywords still match."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": ["xero"], "restore": True})
        assert res.json()["restored"] == 1
        assert res.json()["total_hidden"] == 0

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "New"
        conn.close()


class TestJobDetail:
    def test_found(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(
            conn, job_id=1, job_url="http://test.com/1",
            title="Test Job", company="Corp",
        )
        conn.close()

        res = client.get(f"/api/jobs/{row_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Test Job"
        assert "history" in data

    def test_not_found(self, client):
        res = client.get("/api/jobs/99999")
        assert res.status_code == 404


class TestStatusUpdate:
    def test_valid(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1")
        conn.close()

        res = client.patch(f"/api/jobs/{row_id}/status", json={"status": "Applied"})
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_invalid(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1")
        conn.close()

        res = client.patch(f"/api/jobs/{row_id}/status", json={"status": "Bogus"})
        assert res.status_code == 400


class TestSkills:
    def test_empty(self, client):
        res = client.get("/api/skills")
        assert res.status_code == 200
        assert res.json() == []


class TestIndex:
    def test_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "Job Hunter" in res.text
