"""
tests/test_w_g.py — W-G: run history (P8), soft reset (P6), single-job recheck (P5).

Each of these was a hole in the safety story: no record of what a run did, a
reset that deleted 1,200 rows with no way back, and no way to refresh one job.
"""

from types import SimpleNamespace

import pytest

from db.repos import jobs as job_repo
from db.repos import runs as runs_repo


class _FakeClient:
    def set_stop(self, token):
        self.token = token


def _db():
    """The connection the API itself is using (the client fixture repoints DB_PATH)."""
    from db.connection import get_conn
    return get_conn()


def _seed(conn, job_id=7):
    job_repo.upsert_stub(conn, job_id=job_id, title=f"Job {job_id}",
                         job_url=f"https://onlinejobs.ph/job/{job_id}")
    conn.commit()
    return conn.execute("SELECT id FROM jobs WHERE job_id=?", (job_id,)).fetchone()["id"]


# ── P8: runs history ────────────────────────────────────────────────────────

def test_record_run_status_writes_a_run_row(client):
    conn = _db()
    from app import scheduler
    scheduler.begin_run("r1", scheduler.StopToken(), _FakeClient(),
                        scope={"keyword": "excel"}, kind="check")
    scheduler.record_run_status(conn, stopped=False,
                                summary={"new": 5, "closed": 2, "errors": 1})
    scheduler.end_run("r1")

    items = client.get("/api/runs").json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["kind"] == "check" and row["status"] == "completed"
    assert row["inserted"] == 5 and row["closed"] == 2 and row["errors"] == 1
    assert "excel" in row["scope"]


def test_run_kind_comes_from_the_active_run(client):
    """The call site doesn't repeat the kind — begin_run already knows it."""
    conn = _db()
    from app import scheduler
    scheduler.begin_run("r2", scheduler.StopToken(), _FakeClient(), kind="harvest")
    scheduler.record_run_status(conn, stopped=True)
    scheduler.end_run("r2")
    row = client.get("/api/runs").json()["items"][0]
    assert row["kind"] == "harvest" and row["status"] == "stopped"


def test_failed_run_records_the_error(client):
    conn = _db()
    from app import scheduler
    scheduler.begin_run("r3", scheduler.StopToken(), _FakeClient(), kind="auto")
    scheduler.record_run_status(conn, error="boom")
    scheduler.end_run("r3")
    row = client.get("/api/runs").json()["items"][0]
    assert row["kind"] == "auto" and row["status"] == "failed" and row["error"] == "boom"


def test_recent_caps_the_limit(client):
    conn = _db()
    for i in range(12):
        runs_repo.record(conn, "harvest", "completed", summary={"new": i})
    rows = runs_repo.recent(conn, limit=5)
    assert len(rows) == 5
    assert rows[0]["inserted"] == 11  # newest first


# ── P6: soft reset + undo ───────────────────────────────────────────────────

def test_reset_hides_and_undo_brings_back(client):
    conn = _db()
    _seed(conn, 7)
    _seed(conn, 8)
    assert client.get("/api/stats").json()["total"] == 2

    res = client.post("/api/jobs/reset")
    assert res.json()["deleted_jobs"] == 2 and res.json()["undo"] is True
    assert client.get("/api/stats").json()["total"] == 0
    assert client.get("/api/jobs").json()["total"] == 0

    # The rows are still there — that is the whole point of the Undo button.
    assert client.get("/api/jobs?include_deleted=true").json()["total"] == 2

    assert client.post("/api/jobs/reset/undo").json()["restored_jobs"] == 2
    assert client.get("/api/jobs").json()["total"] == 2


def test_undo_when_nothing_was_reset_is_harmless(client):
    conn = _db()
    _seed(conn, 9)
    assert client.post("/api/jobs/reset/undo").json()["restored_jobs"] == 0
    assert client.get("/api/jobs").json()["total"] == 1


def test_harvest_still_sees_soft_deleted_rows_as_existing(client):
    """A reset must not make the next scrape re-insert the same 1,200 jobs."""
    conn = _db()
    pk = _seed(conn, 10)
    conn.execute("UPDATE jobs SET deleted_at = datetime('now') WHERE id=?", (pk,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert 10 in job_repo.get_existing_job_ids(conn)


# ── P5: recheck one job ─────────────────────────────────────────────────────

@pytest.fixture
def fake_detail(monkeypatch):
    import scraper.parsers as parsers
    seen = {}

    def fake_parse(html, url=""):
        seen["url"] = url
        return SimpleNamespace(title="Fresh title", company="Fresh Co",
                               description="Now with a description", salary="$900/mo",
                               hours_per_week="40", work_type="Full Time",
                               date_updated="", skills=["Excel"], category="",
                               employer_id=None, is_closed=False, close_reason=None)

    monkeypatch.setattr(parsers, "parse_job_detail", fake_parse)

    class Client:
        def get(self, url):
            return SimpleNamespace(status_code=200, text="<html></html>")

    import app.server as srv
    monkeypatch.setattr(srv, "get_client", lambda: Client())
    return seen


def test_recheck_refreshes_one_job(client, fake_detail):
    conn = _db()
    pk = _seed(conn, 11)
    res = client.post(f"/api/jobs/{pk}/recheck")
    assert res.status_code == 200, res.text
    assert res.json()["has_description"] is True
    assert fake_detail["url"].endswith("/job/11")
    row = conn.execute("SELECT description, company FROM jobs WHERE id=?", (pk,)).fetchone()
    assert row["description"] == "Now with a description" and row["company"] == "Fresh Co"


def test_recheck_404_for_unknown_job(client):
    assert client.post("/api/jobs/99999/recheck").status_code == 404


def test_recheck_marks_closed_on_404(client, monkeypatch):
    conn = _db()
    import app.server as srv

    class Client:
        def get(self, url):
            return SimpleNamespace(status_code=404, text="oops, we lost you there")

    monkeypatch.setattr(srv, "get_client", lambda: Client())
    pk = _seed(conn, 12)
    res = client.post(f"/api/jobs/{pk}/recheck")
    assert res.status_code == 200 and res.json()["is_closed"] is True
    scrape = conn.execute("SELECT scrape_status FROM jobs WHERE id=?", (pk,)).fetchone()[0]
    assert scrape == "Closed"
