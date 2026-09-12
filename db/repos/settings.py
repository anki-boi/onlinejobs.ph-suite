"""
db/repos/settings.py — tiny key/value store for app state (auto-run config,
last/next run timestamps, last error) + the inter-process instance lock
(W2.6) that keeps two Job Hunter processes from scraping the site at once.
"""

import json
import os
import sqlite3
import time


BACKUP_RETENTION_KEY = "backup_retention_days"
DEFAULT_BACKUP_RETENTION_DAYS = 7


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    """Get an integer setting (falls back to `default`)."""
    raw = get(conn, key)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def set_int(conn: sqlite3.Connection, key: str, value: int) -> None:
    """Set an integer setting."""
    set(conn, key, str(value))


def set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# ── Inter-process instance lock (W2.6) ──────────────────────────────────────

INSTANCE_LOCK_KEY = "pipeline_lock"
INSTANCE_LOCK_STALE_SECONDS = 300  # no heartbeat for 5 min → holder is hung


def _pid_alive(pid) -> bool:
    """Cross-platform liveness probe. On Windows os.kill(pid, 0) would
    TerminateProcess() the target, so use OpenProcess + GetExitCodeProcess.
    A live process reads as STILL_ACTIVE (255) — except on Windows 11 24H2,
    where it reads 259; both are accepted. A dead pid's exit code is its real
    one; if that happens to be 255/259 the heartbeat expiry (5 min) frees the
    lock anyway. (pid reuse within that window is an accepted ceiling.)"""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False  # non-numeric holder pid (corrupt value) → treat as dead
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE_CODES = {255, 259}
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value in STILL_ACTIVE_CODES
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                       (INSTANCE_LOCK_KEY,)).fetchone()
    if not row or not row["value"]:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None  # corrupt value → treat as no lock, acquire overwrites


def _is_stale(holder: dict) -> bool:
    if not _pid_alive(holder.get("pid")):
        return True  # dead pid (or never ran) — lock is orphaned
    try:
        age = time.time() - float(holder.get("heartbeat", 0))
    except (TypeError, ValueError):
        return True
    return age > INSTANCE_LOCK_STALE_SECONDS


def acquire_instance_lock(conn: sqlite3.Connection, pid: int | None = None) -> bool:
    """Claim the cross-process pipeline lock. Claimed when: absent, ours
    (re-entrant — just refreshes), or the holder is stale. Atomic under
    BEGIN IMMEDIATE so two processes can't both win the race.
    Returns False when a live, fresh holder has it."""
    pid = os.getpid() if pid is None else int(pid)
    conn.execute("BEGIN IMMEDIATE")
    try:
        holder = _read_lock(conn)
        if holder and holder.get("pid") != pid and not _is_stale(holder):
            conn.execute("ROLLBACK")
            return False
        now = time.time()
        started = holder.get("started", now) if holder and holder.get("pid") == pid else now
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (INSTANCE_LOCK_KEY,
             json.dumps({"pid": pid, "started": started, "heartbeat": now})),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def heartbeat_instance_lock(conn: sqlite3.Connection, pid: int | None = None) -> None:
    """Refresh our heartbeat (callers do this per event batch). No-op if we
    don't hold the lock (e.g. run_once called from a test without one)."""
    pid = os.getpid() if pid is None else int(pid)
    holder = _read_lock(conn)
    if not holder or holder.get("pid") != pid:
        return
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = ?",
        (json.dumps({**holder, "heartbeat": time.time()}), INSTANCE_LOCK_KEY),
    )
    conn.commit()


def release_instance_lock(conn: sqlite3.Connection, pid: int | None = None) -> None:
    """Drop the lock if (and only if) we hold it — never release someone
    else's lock after our own went stale."""
    pid = os.getpid() if pid is None else int(pid)
    holder = _read_lock(conn)
    if not holder or holder.get("pid") != pid:
        return
    conn.execute("DELETE FROM app_settings WHERE key = ?", (INSTANCE_LOCK_KEY,))
    conn.commit()


def instance_lock_holder(conn: sqlite3.Connection) -> dict | None:
    """Current holder with a 'stale' flag — for /api/schedule, /health and the
    UI ('another instance is running')."""
    holder = _read_lock(conn)
    if not holder:
        return None
    holder["stale"] = _is_stale(holder)
    return holder
