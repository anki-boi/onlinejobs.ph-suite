"""
tests/test_scheduler.py — auto-run: harvest+enrich in a background thread,
alerts published as events (desktop notifications consume these).

No network: fake client, temp DB. The real OJClient is only touched via the
injected `client_factory`.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import scheduler
from tests.test_parsers import SEARCH_HTML, DETAIL_HTML, CLOSED_HTML
from tests.test_pipeline import FakeResp, _with_data_layer


class SchedulerClient:
    """Page 0 → the shared SEARCH_HTML fixture (2 stubs, dataLayer 2); page 2+ →
    no job boxes (site runs out); detail pages → DETAIL_HTML or CLOSED_HTML."""
    stopped = False
    base_url = "http://x"

    def get(self, url):
        if "/jobsearch/" in url or "/c/" in url:
            return FakeResp(SEARCH_HTML.replace("jobpost-cat-box", ""))
        if url.endswith("/job/closed"):
            return FakeResp(CLOSED_HTML)
        if "jobsearch?" in url or "jobsearch?" in url:
            return FakeResp(SEARCH_HTML)
        return FakeResp(DETAIL_HTML)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "sched.db"))
    c = dbconn.init_db(dbconn.get_conn())
    yield c
    c.close()


def _record():
    events = []
    def publish(event, data):
        events.append((event, data))
    return events, publish


def test_run_once_publishes_new_jobs(conn):
    events, publish = _record()
    summary = scheduler.run_once(SchedulerClient(), conn, publish)
    assert summary["inserted"] == 2
    new_jobs = [d for e, d in events if e == "new_jobs"]
    assert any(d["count"] == 2 for d in new_jobs)


def test_run_once_no_new_jobs_no_new_jobs_event(conn):
    events, publish = _record()
    scheduler.run_once(SchedulerClient(), conn, publish)          # first run inserts
    events.clear()
    summary = scheduler.run_once(SchedulerClient(), conn, publish)  # all known now
    assert summary["inserted"] == 0
    assert "new_jobs" not in [e for e, _ in events]


def test_run_once_alerts_on_structure_change(conn):
    """Harvest watchdog (0 boxes, results claimed) surfaces as a 'structure' alert."""
    bad = _with_data_layer(SEARCH_HTML, 42).replace("jobpost-cat-box", "jobpost-renamed-box")
    class Broken(SchedulerClient):
        def get(self, url):
            return FakeResp(bad)
    events, publish = _record()
    scheduler.run_once(Broken(), conn, publish)
    alerts = [d for e, d in events if e == "alert"]
    assert any(d.get("type") == "structure" and "job boxes parsed" in d.get("message", "") for d in alerts)


def test_run_once_alerts_when_jobs_close(conn):
    events, publish = _record()
    from db.repos import jobs as job_repo
    job_repo.upsert_stub(conn, job_id=9, job_url="http://x/job/closed", title="Doomed")
    summary = scheduler.run_once(SchedulerClient(), conn, publish)
    assert summary["closed"] >= 1
    alerts = [d for e, d in events if e == "alert"]
    assert any(d.get("type") == "jobs_closed" and d.get("count", 0) >= 1 for d in alerts)


def test_run_once_follow_up_alert(conn):
    from db.repos import jobs as job_repo
    row_id, _ = job_repo.upsert_stub(conn, job_id=42, job_url="http://x/job/42", title="Nudge Me")
    job_repo.update_status(conn, row_id, "Applied")
    job_repo.update_follow_up(conn, row_id, "2020-01-01")
    events, publish = _record()
    scheduler.run_once(SchedulerClient(), conn, publish)
    alerts = [d for e, d in events if e == "alert"]
    assert any(d.get("type") == "follow_up" and d.get("count") == 1 for d in alerts)


def test_tick_sets_last_and_next_run(conn, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(scheduler, "run_once", lambda client, c, p: {"inserted": 0})
    before = time.time()
    scheduler.tick(lambda: SchedulerClient())
    from db.repos import settings as settings_repo
    last = settings_repo.get(conn, "last_run")
    nxt = float(settings_repo.get(conn, "next_run"))
    assert last
    assert before + 4 * 3600 - 5 <= nxt <= before + 4 * 3600 + 5   # default 4h out


def test_tick_disabled_does_nothing(conn, monkeypatch):
    from db.repos import settings as settings_repo
    settings_repo.set(conn, "auto_run_enabled", "0")
    monkeypatch.setattr(scheduler, "run_once", lambda *a: pytest.fail("should not run"))
    scheduler.tick(lambda: SchedulerClient())
    assert settings_repo.get(conn, "last_run") is None


def test_tick_respects_next_run(conn, monkeypatch):
    from db.repos import settings as settings_repo
    settings_repo.set(conn, "next_run", str(time.time() + 9999))
    monkeypatch.setattr(scheduler, "run_once", lambda *a: pytest.fail("should not run"))
    scheduler.tick(lambda: SchedulerClient())


def test_tick_skips_while_manual_run_holds_lock(conn, monkeypatch):
    monkeypatch.setattr(scheduler, "run_once", lambda *a: pytest.fail("should not run"))
    assert scheduler.pipeline_lock.acquire(blocking=False)
    try:
        scheduler.tick(lambda: SchedulerClient())
    finally:
        scheduler.pipeline_lock.release()


def test_start_spawns_daemon_thread_and_ticks():
    import db.connection as dbconn
    t = scheduler.start(interval_sec=300, on_tick=lambda: None)
    assert t.daemon
    t = scheduler.start(interval_sec=300, on_tick=lambda: None)
    assert t.daemon


def test_run_once_honors_scrape_scope(conn):
    """A saved scrape keyword limits what the auto-run harvests."""
    from db.repos import settings as settings_repo
    settings_repo.set(conn, "scrape_keyword", "python")
    conn.commit()
    events, publish = _record()
    summary = scheduler.run_once(SchedulerClient(), conn, publish)
    assert summary["inserted"] == 1
    row = conn.execute("SELECT title FROM jobs").fetchone()
    assert "Python" in row["title"]


def test_run_once_auto_applies_saved_keywords(conn):
    """Saved negative keyword auto-hides matching fresh stubs; no manual apply."""
    from db.repos import settings as settings_repo
    import json
    settings_repo.set(conn, "negative_keywords", json.dumps(["python"]))
    conn.commit()
    events, publish = _record()
    summary = scheduler.run_once(SchedulerClient(), conn, publish)
    assert summary["inserted"] == 2
    assert summary["auto_hidden"] == 1
    by_title = {r["title"]: r["filter_hidden"] for r in conn.execute(
        "SELECT title, filter_hidden FROM jobs")}
    assert by_title["Python Developer (Jr)"] == 1
    assert by_title["Digital / Social Media Marketing Director"] == 0
    assert any(e == "alert" and d.get("type") == "keyword_filter" for e, d in events)
