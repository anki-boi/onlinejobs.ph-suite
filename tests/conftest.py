"""
tests/conftest.py — Shared fixtures.
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with a temp DB (shared by API-level tests)."""
    from fastapi.testclient import TestClient
    from app.server import app
    import db.connection as dbconn

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(dbconn, "DB_PATH", db_path)
    dbconn.init_db()
    monkeypatch.setattr("app.server._client", None)

    with TestClient(app) as c:
        yield c


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temp DB and return a connection."""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from db.connection import SCHEMA
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _close_shared_db_conns():
    """W2.1: release thread-shared connections between tests so the registry
    never accumulates handles across the suite."""
    yield
    from app import deps
    deps.close_all()
