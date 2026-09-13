"""W5.2 golden tests — uniform error envelope {"error":{code,message,detail}}.

One test per error class: HTTP errors, 422 validation (names the field),
stray ValueError, sqlite3.OperationalError (locked → 503 db_locked), and
ScrapeStopped (user stop → 409 stopped). The envelope shape is the contract —
every field is asserted.
"""

import sqlite3

from app import server as srv
from scraper.client import ScrapeStopped
from db.repos import jobs as job_repo


def _envelope(r):
    """The whole body must BE the envelope — no extra keys."""
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "detail"}
    return body["error"]


class TestHttpErrors:
    def test_404_unknown_job(self, client):
        r = client.get("/api/jobs/999999")
        assert r.status_code == 404
        e = _envelope(r)
        assert e["code"] == "not_found"
        assert e["message"] == "Job not found"
        assert e["detail"] is None

    def test_400_interval_out_of_range(self, client):
        r = client.post("/api/schedule", json={"interval_hours": 99})
        assert r.status_code == 400
        e = _envelope(r)
        assert e["code"] == "bad_request"
        assert "1-24" in e["message"]
        assert e["detail"] is None

    def test_stop_with_no_active_run_is_a_200_not_an_error(self, client, monkeypatch):
        from app import scheduler
        monkeypatch.setattr(scheduler, "stop_run", lambda _run_id=None: False)
        r = client.post("/api/pipeline/stop", json={})
        assert r.status_code == 200
        assert "error" not in r.json()
        assert "No active run to stop" in r.json()["message"]


class TestValidation422:
    def test_422_names_the_field(self, client):
        r = client.post("/api/keywords/apply",
                        json={"positive": "not-a-list"})  # expects list[str]
        assert r.status_code == 422
        e = _envelope(r)
        assert e["code"] == "validation_error"
        # the message names the field the user can act on
        assert "positive" in e["message"]
        # detail carries the machine-locatable list (loc/msg/type only)
        assert all(set(d) == {"loc", "msg", "type"} for d in e["detail"])


class TestStrayExceptions:
    def test_value_error_maps_to_400_bad_value(self, client, monkeypatch):
        def boom(_conn):
            raise ValueError("hours must be a number, got 'abc'")
        monkeypatch.setattr(job_repo, "get_stats", boom)
        r = client.get("/api/stats")
        assert r.status_code == 400
        e = _envelope(r)
        assert e["code"] == "bad_value"
        assert "abc" in e["message"]

    def test_sqlite_locked_maps_to_503_db_locked(self, client, monkeypatch):
        def locked_db():
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(srv, "get_db", locked_db)
        r = client.get("/api/jobs")
        assert r.status_code == 503
        e = _envelope(r)
        assert e["code"] == "db_locked"
        assert "locked" in e["message"]

    def test_sqlite_non_lock_error_maps_to_500_db_error(self, client, monkeypatch):
        def broken_db():
            raise sqlite3.OperationalError("no such table: jobs")
        monkeypatch.setattr(srv, "get_db", broken_db)
        r = client.get("/api/jobs")
        assert r.status_code == 500
        e = _envelope(r)
        assert e["code"] == "db_error"
        assert e["message"]

    def test_scrape_stopped_maps_to_409_stopped(self, client, monkeypatch):
        def stopped(_client, **_kw):
            raise ScrapeStopped("User requested stop")
        from app.routers import skills as skills_router
        monkeypatch.setattr(skills_router, "fetch_skills", stopped)
        r = client.post("/api/skills/refresh")
        assert r.status_code == 409
        e = _envelope(r)
        assert e["code"] == "stopped"
        assert e["message"]


class TestSuccessShapeUnchanged:
    def test_success_bodies_are_not_enveloped(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        # stats returns {total,...} — never an error key on success
        assert "error" not in body
        assert "total" in body
