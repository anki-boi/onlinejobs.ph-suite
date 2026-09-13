"""
tests/test_w42.py — W4.2: numeric salary filtering (PHP-normalized) + currency filter.

The user's mental model is "how much money per month", so filters operate on the
PHP-normalized columns (W4.1), not on the raw mixed-currency text.
"""

import sqlite3

import db.connection as dbconn
from app import config as appconfig
from db.repos import jobs as job_repo

ROWS = [
    # (job_id, url, title, salary text, expected monthly)
    (1, "a", "A", "₱60,000/mo", 60000.0),
    (2, "b", "B", "US$800/mo", 46400.0),
    (3, "c", "C", "TBD", None),
    (4, "d", "D", "₱20,000/mo", 20000.0),
    (5, "e", "E", "US$500-$700/mo", None),  # 29000-40600
]


def _fresh(tmp_path, monkeypatch):
    dbpath = tmp_path / "w42.db"
    monkeypatch.setattr(dbconn, "DB_PATH", dbpath)
    monkeypatch.setattr(appconfig, "_live", {"fx_to_php": {"usd": 58.0}})
    monkeypatch.setattr(appconfig, "_read_disk", lambda: {"fx_to_php": {"usd": 58.0}})
    dbconn.init_db()
    conn = sqlite3.connect(str(dbpath))
    conn.row_factory = sqlite3.Row
    for jid, url, title, sal, _ in ROWS:
        job_repo.upsert_stub(conn, job_id=jid, job_url=f"https://x/{url}", title=title, salary=sal)
    return conn


def _urls(conn, **kw):
    rows, _ = job_repo.get_jobs(conn, **kw)
    return [r["title"] for r in rows]


def test_min_monthly_filter(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    assert _urls(conn, salary_min_monthly=50000) == ["A"]
    # default order is newest-first, so later inserts sort ahead
    assert _urls(conn, salary_min_monthly=46400) == ["B", "A"]
    assert _urls(conn, salary_min_monthly=1000) == ["E", "D", "B", "A"]  # TBD out


def test_max_monthly_filter(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    # COALESCE(min, max): B's 46400 <= 50000 in; A's 60000 out; TBD never in
    assert _urls(conn, salary_max_monthly=50000) == ["E", "D", "B"]
    # range: B (46400) and E (max 40600 >= 40000, min 29000 <= 50000) both qualify
    assert _urls(conn, salary_min_monthly=40000, salary_max_monthly=50000) == ["E", "B"]


def test_currency_filter(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    # default ordering is newest-first (id desc for same-second inserts)
    assert _urls(conn, salary_currency="USD") == ["E", "B"]
    assert _urls(conn, salary_currency="PHP") == ["D", "A"]
    assert _urls(conn, salary_currency="PHP,USD") == ["E", "D", "B", "A"]


def test_filters_combine_with_existing(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    assert _urls(conn, salary_min_monthly=1000, salary_currency="USD", has_salary=True) == ["E", "B"]


def test_salary_sort_survives_filters(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    rows, _ = job_repo.get_jobs(conn, salary_min_monthly=1000, sort="salary", order="desc")
    assert [r["title"] for r in rows] == ["A", "B", "E", "D"]  # 60000, 46400, 40600, 20000


def test_api_salary_filters(client):
    from db.connection import get_conn
    conn = get_conn()
    for jid, url, title, sal, _ in ROWS:
        job_repo.upsert_stub(conn, job_id=jid, job_url=f"https://x/{url}", title=title, salary=sal)
    res = client.get("/api/jobs", params={"salary_min_monthly": 50000})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1 and data["items"][0]["title"] == "A"
    res = client.get("/api/jobs", params={"salary_currency": "USD", "salary_max_monthly": 50000})
    assert sorted(j["title"] for j in res.json()["items"]) == ["B", "E"]
    # FastAPI validates the float — garbage is a 422, not a 500
    assert client.get("/api/jobs", params={"salary_min_monthly": "lots"}).status_code == 422
