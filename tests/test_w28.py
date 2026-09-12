"""
tests/test_w28.py — Per-run stop tokens (W2.8).

Acceptance:
- stop() targets a specific run: one run's stop flips only that run's token;
  other runs (and the next run) keep going.
- A stopped run records status 'stopped' with its partial results — never
  'failed'. Only a raised error is 'failed'.
- The stale-stop leak is gone: a stopped run's dead token never leaks into the
  next run (the client is re-pointed at a fresh token each run).
- The stop button's legacy no-body call still stops the active run.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import scheduler  # noqa: E402
from scraper.client import OJClient, ScrapeStopped, StopToken  # noqa: E402
from scraper import pipeline as P  # noqa: E402
from tests.test_pipeline import FakeResp  # noqa: E402
from tests.test_parsers import SEARCH_HTML  # noqa: E402


# ── Fake clients that delegate to a real StopToken (like OJClient does) ─────


class TokenClient:
    """Minimal client shaped like OJClient: a swappable stop token, a
    `stopped` property that delegates to it, and a scripted get()."""

    def __init__(self, pages, detail_html=""):
        self.base_url = "http://x"
        self.token = StopToken()
        self._stop_token = self.token
        self._pages = list(pages)          # html to return per get() call
        self.detail_html = detail_html
        self.get_calls = 0

    def set_stop(self, token):
        self._stop_token = token

    @property
    def stopped(self):
        return self._stop_token.stopped

    def reset(self):
        self._stop_token.reset()

    def get(self, url):
        if self._stop_token.stopped:
            raise ScrapeStopped("stop")
        self.get_calls += 1
        if self._pages:
            body = self._pages.pop(0)
        else:
            body = self.detail_html
        return FakeResp(text=body, status_code=200)


def _stopper_after_first_get(pages, detail_html=""):
    """Client whose 1st get() returns the first page, then flips the run's
    CURRENT stop token (user pressed Stop mid-run); later gets raise
    ScrapeStopped. Stops _stop_token (not the constructor token) because
    begin_run() re-points the client at the run's own token."""
    c = TokenClient(pages, detail_html=detail_html)
    real_get = c.get

    def get(url):
        if c.get_calls == 0:
            resp = real_get(url)
            c._stop_token.stop()        # Stop pressed right after page 1
            return resp
        return real_get(url)

    c.get = get
    return c


# ── 1. StopToken + OJClient delegation ──────────────────────────────────────


class TestStopToken:
    def test_set_reset(self):
        t = StopToken()
        assert not t.stopped
        t.stop()
        assert t.stopped
        t.stop()                          # idempotent
        t.reset()
        assert not t.stopped

    def test_client_stop_reset_stopped_delegate(self):
        c = OJClient(base_url="http://x")
        assert not c.stopped
        c.stop()
        assert c.stopped
        c.reset()
        assert not c.stopped

    def test_set_stop_swaps_the_token(self):
        c = OJClient(base_url="http://x")
        other = StopToken()
        c.set_stop(other)
        other.stop()
        assert c.stopped is True
        t = StopToken()
        c.set_stop(t)
        assert c.stopped is False         # the old stop no longer leaks


# ── 2. Registry: stop one run, never another ────────────────────────────────


class TestRunRegistry:
    def test_stop_explicit_id_only_flips_that_run(self):
        a = StopToken()
        b = StopToken()
        scheduler.register_run("aaa", a)
        scheduler.register_run("bbb", b)
        assert scheduler.stop_run("bbb") is True
        assert b.stopped and not a.stopped
        scheduler.end_run("aaa")
        scheduler.end_run("bbb")

    def test_stop_none_flips_the_active_run(self):
        a = StopToken()
        b = StopToken()
        scheduler.register_run("aaa", a)
        scheduler.register_run("bbb", b)
        scheduler.active_run_id = "bbb"
        assert scheduler.stop_run(None) is True
        assert b.stopped and not a.stopped
        scheduler.end_run("aaa")
        scheduler.end_run("bbb")
        scheduler.active_run_id = None

    def test_stop_unknown_id_no_flip(self):
        assert scheduler.stop_run("nope") is False

    def test_begin_run_points_client_at_fresh_token(self):
        c = TokenClient(pages=[""])
        t1 = StopToken()
        scheduler.begin_run("r1", t1, c)
        t1.stop()
        assert c.stopped is True          # run 1 is stopped
        t2 = StopToken()
        scheduler.begin_run("r2", t2, c)
        assert c.stopped is False         # run 2 gets a fresh flag — no leak
        scheduler.end_run("r1")
        scheduler.end_run("r2")

    def test_end_run_drops_token(self):
        t = StopToken()
        scheduler.register_run("zzz", t)
        scheduler.end_run("zzz")
        assert "zzz" not in scheduler.active_runs


# ── 3. Pipeline summaries mark stopped ──────────────────────────────────────


class TestStoppedSummaries:
    def test_harvest_stopped_pre_flipped_records_stopped(self):
        c = TokenClient(pages=[SEARCH_HTML])
        c.token.stop()
        events = list(P.harvest(c, keyword="", existing_ids=set()))
        assert events[-1].type == "summary"
        assert events[-1].data["stopped"] is True
        assert events[-1].data["new"] == 0    # nothing harvested this run

    def test_harvest_stopped_mid_run_keeps_partial_results(self):
        c = _stopper_after_first_get(pages=[SEARCH_HTML, ""])
        events = list(P.harvest(c, keyword="", existing_ids=set()))
        summary = [e for e in events if e.type == "summary"][-1]
        assert summary.data["stopped"] is True
        assert summary.data["new"] == 2       # page 1's results kept
        stubs = [e for e in events if e.type == "harvest_result"]
        assert len(stubs) == 1

    def test_enrich_stopped_pre_flipped_records_stopped(self):
        c = TokenClient(pages=[])
        c.token.stop()
        events = list(P.enrich(c, [(1, "http://x/1"), (2, "http://x/2")], workers=1))
        summary = [e for e in events if e.type == "summary"][-1]
        assert summary.data["stopped"] is True
        assert summary.data["open"] == 0 and summary.data["errors"] == 0

    def test_enrich_running_records_not_stopped(self):
        from tests.test_parsers import DETAIL_HTML
        c = TokenClient(pages=[DETAIL_HTML])
        events = list(P.enrich(c, [(1, "http://x/1")], workers=1))
        summary = [e for e in events if e.type == "summary"][-1]
        assert summary.data["stopped"] is False


# ── 4. Auto-run: status recording + no leak across ticks ────────────────────


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "test.db"))
    dbconn.init_db()
    return dbconn.get_conn()


class TestAutoRunStatus:
    def test_stopped_tick_records_stopped_not_failed(self, conn):
        from db.repos import settings as settings_repo
        c = _stopper_after_first_get(pages=[SEARCH_HTML])
        scheduler.tick(client_factory=lambda: c)
        assert settings_repo.get(conn, "last_status") == "stopped"
        assert settings_repo.get(conn, "last_error") == ""

    def test_next_tick_after_a_stopped_run_completes(self, conn):
        """The stale-stop leak: the auto-run after a stopped run must not
        inherit its dead stop flag."""
        from db.repos import settings as settings_repo

        stopped = _stopper_after_first_get(pages=[SEARCH_HTML])
        scheduler.tick(client_factory=lambda: stopped)
        assert settings_repo.get(conn, "last_status") == "stopped"
        settings_repo.set(conn, "next_run", "")   # allow the next tick to fire

        fresh = TokenClient(pages=[SEARCH_HTML])
        scheduler.tick(client_factory=lambda: fresh)
        assert settings_repo.get(conn, "last_status") == "completed"
        assert settings_repo.get(conn, "last_error") == ""

    def test_failed_tick_records_failed(self, conn):
        from db.repos import settings as settings_repo

        class BoomClient:
            base_url = "http://x"
            def set_stop(self, t):
                pass
            @property
            def stopped(self):   # raises outside harvest's per-page try → tick sees it
                raise RuntimeError("boom")
            def get(self, url):
                raise RuntimeError("boom")

        scheduler.tick(client_factory=lambda: BoomClient())
        assert settings_repo.get(conn, "last_status") == "failed"
        assert "boom" in settings_repo.get(conn, "last_error")

    def test_tick_clears_the_active_run_when_done(self, conn):
        from db.repos import settings as settings_repo
        settings_repo.set(conn, "next_run", "")
        c = TokenClient(pages=[SEARCH_HTML])
        seen = {}
        real_get = c.get

        def get(url):
            seen["active"] = scheduler.active_run_id   # mid-run: token is bound
            return real_get(url)

        c.get = get
        scheduler.tick(client_factory=lambda: c)
        assert scheduler.active_run_id is None
        assert seen["active"] is not None    # it was set while the run was in


# ── 5. HTTP: stop endpoint + stopped streams ────────────────────────────────


def _events(text):
    """Parse raw SSE text into (event, data) pairs — data is JSON when the
    payload is, the raw string otherwise (e.g. done "stopped")."""
    import json
    out, ev, data = [], None, []
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
        elif line == "":
            if ev is not None:
                raw = "\n".join(data)
                try:
                    out.append((ev, json.loads(raw)))
                except ValueError:
                    out.append((ev, raw))
            ev, data = None, []
    return out


class TestStopEndpoint:
    def test_stop_targets_run_id(self, client):
        a, b = StopToken(), StopToken()
        scheduler.register_run("aaa", a)
        scheduler.register_run("bbb", b)
        scheduler.active_run_id = "aaa"
        try:
            res = client.post("/api/pipeline/stop", json={"run_id": "bbb"})
            assert res.status_code == 200
            assert b.stopped and not a.stopped
        finally:
            scheduler.end_run("aaa")
            scheduler.end_run("bbb")
            scheduler.active_run_id = None

    def test_stop_no_body_stops_active_run(self, client):
        a = StopToken()
        scheduler.register_run("aaa", a)
        scheduler.active_run_id = "aaa"
        try:
            res = client.post("/api/pipeline/stop")
            assert res.status_code == 200
            assert a.stopped
        finally:
            scheduler.end_run("aaa")
            scheduler.active_run_id = None

    def test_stop_unknown_run_reports_none_active(self, client):
        a = StopToken()
        scheduler.register_run("aaa", a)
        scheduler.active_run_id = "aaa"
        try:
            res = client.post("/api/pipeline/stop", json={"run_id": "zzz"})
            assert res.status_code == 200
            assert "No active run" in res.json()["message"]
            assert not a.stopped          # F1: a stale id must not stop a live run
        finally:
            scheduler.end_run("aaa")
            scheduler.active_run_id = None


class TestStoppedStreams:
    def test_run_stream_ends_stopped_with_partial_jobs(self, client, monkeypatch):
        import app.server as server
        c = _stopper_after_first_get(pages=[SEARCH_HTML, ""])
        monkeypatch.setattr(server, "_client", c)
        res = client.post("/api/pipeline/run", json={})
        assert res.status_code == 200
        evs = _events(res.text)
        done = [d for e, d in evs if e == "done"]
        assert done == ["stopped"]
        assert "failed" not in [d for e, d in evs if e == "done"]
        stubs = [d for e, d in evs if e == "harvest_stub"]
        assert len(stubs) == 2                       # partial results survived

        import db.connection as dbconn
        from db.repos import settings as settings_repo
        conn = dbconn.get_conn()
        assert settings_repo.get(conn, "last_status") == "stopped"
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert n == 2
        assert scheduler.active_run_id is None
        conn.close()

    def test_check_stream_ends_stopped(self, client, monkeypatch):
        import app.server as server
        from tests.test_parsers import DETAIL_HTML
        from db.repos import jobs as job_repo

        import db.connection as dbconn
        conn = dbconn.get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="https://www.onlinejobs.ph/job/1", title="T1")
        conn.close()

        c = _stopper_after_first_get(pages=[DETAIL_HTML])
        monkeypatch.setattr(server, "_client", c)
        res = client.post("/api/pipeline/check", json={})
        evs = _events(res.text)
        done = [d for e, d in evs if e == "done"]
        assert done == ["stopped"]

        conn = dbconn.get_conn()
        from db.repos import settings as settings_repo
        assert settings_repo.get(conn, "last_status") == "stopped"
        conn.close()

    def test_run_stream_run_id_is_the_registered_one(self, client, monkeypatch):
        import app.server as server
        c = _stopper_after_first_get(pages=[SEARCH_HTML, ""])
        monkeypatch.setattr(server, "_client", c)
        res = client.post("/api/pipeline/run", json={})
        evs = _events(res.text)
        started = [d for e, d in evs if e == "run_started"]
        assert started and isinstance(started[0].get("run_id"), str)
        assert len(started[0]["run_id"]) == 32        # the announced id is real

    def test_run_stream_exception_records_failed_not_stopped(self, client, monkeypatch):
        import app.server as server

        class BoomClient:
            base_url = "http://x"
            def set_stop(self, t):
                pass
            @property
            def stopped(self):
                raise RuntimeError("boom")
            def get(self, url):
                raise RuntimeError("boom")

        monkeypatch.setattr(server, "_client", BoomClient())
        res = client.post("/api/pipeline/run", json={})
        evs = _events(res.text)
        done = [d for e, d in evs if e == "done"]
        assert done == ["failed"]

        import db.connection as dbconn
        from db.repos import settings as settings_repo
        conn = dbconn.get_conn()
        assert settings_repo.get(conn, "last_status") == "failed"
        assert "boom" in settings_repo.get(conn, "last_error")
        conn.close()
