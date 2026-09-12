"""tests/test_w29.py — W2.9 graceful shutdown: Ctrl-C stops the in-flight run
and the process ends with a summary line.

Tested at the wiring level (uvicorn.Server.handle_exit, the hook main.py
installs): a real Ctrl-C mid-scrape is the same code path — uvicorn routes
SIGINT to server.handle_exit, which our wrapper intercepts first.
"""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import main as M
from app import scheduler
from scraper.client import StopToken


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "w29.db"))
    c = dbconn.init_db(dbconn.get_conn())
    yield c
    c.close()


def _server():
    return M.make_server("127.0.0.1", 8399)


class _FakeClient:
    base_url = "http://x"

    def set_stop(self, t):
        self._t = t

    @property
    def stopped(self):   # live delegation, like the real OJClient
        return self._t.stopped


def test_server_has_the_10s_force_cap():
    s = _server()
    assert s.config.timeout_graceful_shutdown == 10


def test_ctrl_c_signals_the_inflight_run():
    s = _server()
    token = StopToken()
    client = _FakeClient()
    scheduler.begin_run("rid", token, client)
    try:
        s.handle_exit(signal.SIGINT, None)
        assert token.stopped                 # the run is told to stop
        assert client.stopped                # the shared client sees it
        assert s.should_exit                 # uvicorn proceeds with the drain
        assert not s.force_exit              # first Ctrl-C is graceful
    finally:
        scheduler.end_run("rid")


def test_ctrl_c_with_no_run_is_a_plain_graceful_exit():
    s = _server()
    s.handle_exit(signal.SIGINT, None)
    assert s.should_exit
    assert not s.force_exit


def test_second_ctrl_c_forces_exit():
    s = _server()
    s.handle_exit(signal.SIGINT, None)
    s.handle_exit(signal.SIGINT, None)
    assert s.force_exit


def test_wait_runs_returns_true_when_nothing_is_running():
    assert scheduler.wait_runs(1.0) is True


def test_wait_runs_waits_for_a_finishing_run():
    import threading
    token = StopToken()
    scheduler.begin_run("rid", token, _FakeClient())
    threading.Timer(0.3, lambda: scheduler.end_run("rid")).start()
    assert scheduler.wait_runs(5.0) is True


def test_wait_runs_times_out_on_a_stuck_run():
    token = StopToken()
    scheduler.begin_run("rid", token, _FakeClient())
    try:
        assert scheduler.wait_runs(0.5) is False   # still registered -> False
    finally:
        scheduler.end_run("rid")


def test_shutdown_summary_reports_stopped_partial_run(conn, capsys):
    from db.repos import settings as settings_repo

    settings_repo.set(conn, "last_run", "2026-02-24 10:00:00")
    settings_repo.set(conn, "last_status", "stopped")

    M.shutdown_summary()
    out = capsys.readouterr().out
    assert "2026-02-24 10:00:00" in out
    assert "stopped" in out and "partial results" in out


def test_shutdown_summary_failed_run_shows_the_error(conn, capsys):
    from db.repos import settings as settings_repo

    settings_repo.set(conn, "last_run", "2026-02-24 10:00:00")
    settings_repo.set(conn, "last_status", "failed")
    settings_repo.set(conn, "last_error", "boom")

    M.shutdown_summary()
    out = capsys.readouterr().out
    assert "failed: boom" in out


def test_shutdown_summary_never_ran(conn, capsys):
    M.shutdown_summary()
    assert "never" in capsys.readouterr().out
