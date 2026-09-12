"""
app/deps.py — shared dependencies for server endpoints.

get_db() returns ONE SQLite connection per (worker thread, database path).
SQLite connections are not thread-safe, so each worker thread keeps its own
connection and reuses it across requests — the open-connection count stays
flat no matter how many requests arrive (W2.1). close_all() runs on
shutdown so no handle outlives the process.
"""

import sqlite3
import threading

import db.connection as dbconn

_lock = threading.Lock()
# (thread id, db path) -> connection
_conns: dict[tuple[int, str], sqlite3.Connection] = {}
# db paths whose schema has been ensured in this process
_initialized: set[str] = set()


def _close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _alive(conn: sqlite3.Connection) -> bool:
    """True unless the connection was closed out from under us. sqlite3
    has no `.closed` flag — probe it. A busy DB (OperationalError) still
    counts as alive; only a closed one (ProgrammingError) gets replaced."""
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False
    except sqlite3.Error:
        return True


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """The shared connection for this thread and database (opens on first use).

    The schema is ensured once per process per db path (idempotent, same
    semantics as the old per-request init check — a changed JOBS_DB_PATH
    re-initialises).
    """
    path = str(dbconn.get_db_path(db_path))
    key = (threading.get_ident(), path)
    conn = _conns.get(key)  # plain dict read: atomic under the GIL
    if conn is not None and not _alive(conn):
        with _lock:  # drop it only if it's still the entry we probed
            if _conns.get(key) is conn:
                _close(_conns.pop(key, None))
        conn = None
    if conn is not None:
        return conn
    with _lock:
        if path not in _initialized:
            _initialized.add(path)
            dbconn.init_db(dbconn.get_conn(path))
        conn = _conns.get(key)
        if conn is None:
            # LRU for this thread: switching db paths closes the previous
            # conn, so a thread never holds more than one live connection.
            for old_key in [k for k in _conns if k[0] == key[0]]:
                _close(_conns.pop(old_key))
            conn = dbconn.get_conn(path)
            _conns[key] = conn
        return conn


def close_all() -> None:
    """Close every connection opened through get_db() (shutdown hook)."""
    with _lock:
        for conn in list(_conns.values()):
            _close(conn)
        _conns.clear()
