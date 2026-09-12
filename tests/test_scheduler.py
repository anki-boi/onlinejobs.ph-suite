"""
tests/test_scheduler.py — auto-run: harvest+enrich in a background thread,
alerts published as events (desktop notifications consume these).

No network: fake client, temp DB. The real OJClient is only touched via the
injected `client_factory`.
"""

import sys
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

    def set_stop(self, token):
        # W2.8: mirror the real client — the run's token drives stopped.
        self.stopped = token.stopped

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
    monkeypatch.setattr(scheduler, "run_once", lambda *a: {"inserted": 0})
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


class TestW25SchedulerHonorsConfig:
    """W2.5: run_once takes the live config — enrich_workers and
    enrich_interval_days from config.local.json are honored, no restart."""

    def test_enrich_workers_from_cfg(self, conn, monkeypatch):
        calls = {}
        def fake_enrich(client, jobs, workers=3):
            calls["workers"] = workers
            return iter(())
        monkeypatch.setattr(scheduler, "enrich", fake_enrich)
        _events, publish = _record()
        scheduler.run_once(SchedulerClient(), conn, publish, {"enrich_workers": 1})
        assert calls["workers"] == 1

    def test_enrich_interval_days_from_cfg(self, conn, monkeypatch):
        from db.repos import jobs as job_repo
        row_id, _ = job_repo.upsert_stub(conn, job_id=77, job_url="http://x/job/77", title="Stale")
        conn.execute("UPDATE jobs SET last_checked = datetime('now', '-10 days') WHERE id = ?", (row_id,))
        conn.commit()
        seen = {}
        def fake_enrich(client, jobs, workers=3):
            seen["jobs"] = jobs
            return iter(())
        monkeypatch.setattr(scheduler, "enrich", fake_enrich)
        _events, publish = _record()
        # 10 days old: stale under the default 7d window…
        scheduler.run_once(SchedulerClient(), conn, publish, {})
        assert any(jid == row_id for jid, _u in seen["jobs"])
        # …not under a 30d window
        seen.clear()
        scheduler.run_once(SchedulerClient(), conn, publish, {"enrich_interval_days": 30})
        assert not any(jid == row_id for jid, _u in seen.get("jobs", []))

    def test_tick_passes_live_config(self, conn, monkeypatch):
        import db.connection as dbconn
        got = {}
        monkeypatch.setattr(scheduler, "run_once",
                            lambda client, c, p, cfg=None: got.setdefault("cfg", cfg) or {})
        monkeypatch.setattr(dbconn, "load_config", lambda: {"enrich_workers": 9})
        scheduler.tick(lambda: SchedulerClient())
        assert got["cfg"] == {"enrich_workers": 9}

    def test_run_once_defaults_without_cfg(self, conn, monkeypatch):
        # backward compat: 3-arg call still works (cfg defaults to {})
        calls = {}
        def fake_enrich(client, jobs, workers=3):
            calls["workers"] = workers
            return iter(())
        monkeypatch.setattr(scheduler, "enrich", fake_enrich)
        _events, publish = _record()
        scheduler.run_once(SchedulerClient(), conn, publish)
        assert calls["workers"] == 3
