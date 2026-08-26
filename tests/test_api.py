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
