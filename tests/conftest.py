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
