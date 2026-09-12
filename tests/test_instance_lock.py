"""
tests/test_instance_lock.py — W2.6: inter-process single-flight guard.
Two Job Hunter processes sharing one DB must not scrape the site at once.

The "second process" is simulated with a child python process (its pid is
a live, foreign one) and with a short-lived child (a dead pid).
"""

import json
import os
import subprocess
import sys
import time

import pytest

from db.repos import settings as settings_repo

from tests.test_scheduler import SchedulerClient


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "lock.db"))
    c = dbconn.init_db(dbconn.get_conn())
    yield c
    c.close()


@pytest.fixture
def live_pid():
    """A live process that is not us."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield p.pid
    p.terminate()
    p.wait()


@pytest.fixture
def dead_pid():
    """A pid we know is dead: a short-lived child, reaped (handle closed)."""
    p = subprocess.Popen([sys.executable, "-c", "import time"],
                         stdout=subprocess.DEVNULL)
    p.wait()
    # p's parent-side handle is closed by its __del__ (single close — no GC
    # warning); the dead pid then reports a real exit code, i.e. 'dead'.
    time.sleep(0.2)
    return p.pid


class TestAcquireRelease:
    def test_acquire_reentrant_and_release(self, conn):
        assert settings_repo.acquire_instance_lock(conn) is True
        assert settings_repo.acquire_instance_lock(conn) is True  # ours, re-entrant
        h = settings_repo.instance_lock_holder(conn)
        assert h["pid"] == __import__("os").getpid() and h["stale"] is False
        settings_repo.release_instance_lock(conn)
        assert settings_repo.instance_lock_holder(conn) is None

    def test_foreign_live_holder_blocks_us(self, conn, live_pid):
        assert settings_repo.acquire_instance_lock(conn, pid=live_pid) is True
        assert settings_repo.acquire_instance_lock(conn) is False  # we don't hold it
        h = settings_repo.instance_lock_holder(conn)
        assert h["pid"] == live_pid and h["stale"] is False
        # release is owner-only: ours doesn't touch their lock…
        settings_repo.release_instance_lock(conn)
        assert settings_repo.instance_lock_holder(conn) is not None
        # …theirs does
        settings_repo.release_instance_lock(conn, pid=live_pid)
        assert settings_repo.instance_lock_holder(conn) is None

    def test_dead_holder_is_stale_and_stealable(self, conn, dead_pid):
        settings_repo.acquire_instance_lock(conn, pid=dead_pid)
        h = settings_repo.instance_lock_holder(conn)
        assert h["stale"] is True
        assert settings_repo.acquire_instance_lock(conn) is True  # steals it
        assert settings_repo.instance_lock_holder(conn)["pid"] == __import__("os").getpid()

    def test_alive_but_quiet_holder_is_stale(self, conn, live_pid):
        settings_repo.acquire_instance_lock(conn, pid=live_pid)
        # backdate the heartbeat past the 5-minute staleness window
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (settings_repo.INSTANCE_LOCK_KEY,)
        ).fetchone()
        lock = json.loads(row["value"])
        lock["heartbeat"] = time.time() - 400
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = ?",
            (json.dumps(lock), settings_repo.INSTANCE_LOCK_KEY),
        )
        conn.commit()
        assert settings_repo.instance_lock_holder(conn)["stale"] is True
        # a foreign live process can steal it
        assert settings_repo.acquire_instance_lock(conn, pid=os.getpid()) is True

    def test_heartbeat_refreshes(self, conn):
        settings_repo.acquire_instance_lock(conn, pid=os.getpid())
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (settings_repo.INSTANCE_LOCK_KEY,)
        ).fetchone()
        lock = json.loads(row["value"])
        lock["heartbeat"] = time.time() - 400
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = ?",
            (json.dumps(lock), settings_repo.INSTANCE_LOCK_KEY),
        )
        conn.commit()
        settings_repo.heartbeat_instance_lock(conn, pid=os.getpid())
        h = settings_repo.instance_lock_holder(conn)
        assert h["stale"] is False
        assert time.time() - h["heartbeat"] < 5

    def test_corrupt_lock_value_is_treated_as_absent(self, conn):
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (settings_repo.INSTANCE_LOCK_KEY, "not-json"),
        )
        conn.commit()
        assert settings_repo.instance_lock_holder(conn) is None
        assert settings_repo.acquire_instance_lock(conn) is True

    def test_pid_reuse_ceiling_documented(self, conn, live_pid):
        """Accepted ceiling: a pid reuse within the 5-min window is read as
        'still alive'. Just pin the probe's behavior on a live foreign pid."""
        assert settings_repo._pid_alive(live_pid) is True


class TestSchedulerTickGuard:
    def test_tick_skips_when_foreign_instance_holds(self, conn, live_pid, monkeypatch):
        from app import scheduler
        settings_repo.acquire_instance_lock(conn, pid=live_pid)
        calls = []
        monkeypatch.setattr(scheduler, "run_once",
                            lambda *a: calls.append(1) or {"inserted": 0})
        scheduler.tick(lambda: SchedulerClient())
        assert calls == []  # no auto-run while the other instance is live
        # …and tick didn't touch the foreign lock
        assert settings_repo.instance_lock_holder(conn)["pid"] == live_pid

    def test_tick_runs_when_no_foreign_holder(self, conn, monkeypatch):
        from app import scheduler
        calls = []
        monkeypatch.setattr(scheduler, "run_once",
                            lambda *a: calls.append(1) or {"inserted": 0})
        scheduler.tick(lambda: SchedulerClient())
        assert calls == [1]
        assert settings_repo.instance_lock_holder(conn) is None  # released after run

    def test_stale_foreign_holder_does_not_block_tick(self, conn, dead_pid, monkeypatch):
        from app import scheduler
        settings_repo.acquire_instance_lock(conn, pid=dead_pid)  # dead → stale
        calls = []
        monkeypatch.setattr(scheduler, "run_once",
                            lambda *a: calls.append(1) or {"inserted": 0})
        scheduler.tick(lambda: SchedulerClient())
        assert calls == [1]


class TestManualEndpointsGuard:
    def _stream(self, resp):
        events = []
        for chunk in resp.iter_lines():
            if chunk:
                events.append(chunk.decode())
        return events

    def test_check_reports_foreign_instance(self, client, live_pid):
        settings_repo.acquire_instance_lock(self._db(client), pid=live_pid)
        r = client.post("/api/pipeline/check", json={})
        text = r.text
        assert "another instance is running the pipeline (pid " in text
        assert "done" in text
        assert "busy" in text

    def test_check_runs_when_lock_is_free(self, client):
        r = client.post("/api/pipeline/check", json={})
        assert "nothing to check" in r.text or "done" in r.text

    @staticmethod
    def _db(client):
        # the shared per-thread connection the endpoint uses
        from app import deps
        import db.connection as dbconn
        return deps.get_db(dbconn.DB_PATH)
