"""
tests/test_settings.py — app_settings key/value store (auto-run state, etc.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from db.repos import settings


@pytest.fixture
def conn(tmp_db):
    return tmp_db


def test_get_missing_returns_default(conn):
    assert settings.get(conn, "nope") is None
    assert settings.get(conn, "nope", "fallback") == "fallback"


def test_set_get_roundtrip(conn):
    settings.set(conn, "auto_run_enabled", "0")
    assert settings.get(conn, "auto_run_enabled") == "0"


def test_set_overwrites(conn):
    settings.set(conn, "auto_run_interval_hours", "4")
    settings.set(conn, "auto_run_interval_hours", "8")
    assert settings.get(conn, "auto_run_interval_hours") == "8"


def test_set_accepts_ints(conn):
    settings.set(conn, "nxt", 1234)
    assert settings.get(conn, "nxt") == "1234"
