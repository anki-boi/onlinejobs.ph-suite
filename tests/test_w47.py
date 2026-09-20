"""
tests/test_w47.py — W4.7: EXPLAIN QUERY PLAN snapshot for the main list
queries. Acceptance: no full scan on the default-order and status/scrape
list queries; salary floor uses the index. (The min_ats scan is inherent
to the EXISTS shape — the subquery side stays index-backed.)
"""

import sqlite3

import db.connection as dbconn


def _explain(conn, sql, params=()):
    out = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(r[3] for r in out)


def _seeded_conn(tmp_path, monkeypatch):
    dbpath = tmp_path / "w47.db"
    monkeypatch.setattr(dbconn, "DB_PATH", dbpath)
    dbconn.init_db()
    conn = sqlite3.connect(str(dbpath))
    import time
    now = time.time()
    conn.executemany(
        "INSERT INTO jobs (job_url, job_id, title, company, salary, "
        "salary_monthly_min, salary_monthly_max, date_found, scrape_status, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,'new')",
        [(f"http://oj/{i}", i, f"Job {i}", "Co", "USD 5000/mo",
          200000 + i, 250000 + i, int((now - i * 3600) * 1000), "ok")
         for i in range(500)],
    )
    conn.commit()
    return conn


def test_v7_migration_idempotent(tmp_path, monkeypatch):
    import db.migrate as dbmigrate
    monkeypatch.setattr(dbconn, "DB_PATH", tmp_path / "w47b.db")
    dbconn.init_db()
    conn = sqlite3.connect(str(tmp_path / "w47b.db"))
    dbmigrate.MIGRATIONS[7](conn)
    dbmigrate.MIGRATIONS[7](conn)  # runs twice safely
    conn.close()


def test_default_order_uses_index(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path, monkeypatch)
    plan = _explain(conn, "SELECT * FROM jobs ORDER BY date_found DESC, id DESC LIMIT 50 OFFSET 0")
    assert "idx_jobs_date_found" in plan
    assert "TEMP B-TREE" not in plan  # the sort is gone


def test_status_filter_uses_composite_index(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path, monkeypatch)
    plan = _explain(conn, "SELECT * FROM jobs WHERE status='new' "
                          "ORDER BY date_found DESC, id DESC LIMIT 50 OFFSET 0")
    assert "idx_jobs_status_datefound" in plan
    assert "TEMP B-TREE" not in plan


def test_scrape_status_filter_uses_composite_index(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path, monkeypatch)
    plan = _explain(conn, "SELECT * FROM jobs WHERE scrape_status='ok' "
                          "ORDER BY date_found DESC, id DESC LIMIT 50 OFFSET 0")
    assert "idx_jobs_scrape_status_datefound" in plan
    assert "TEMP B-TREE" not in plan


def test_salary_floor_uses_index(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path, monkeypatch)
    plan = _explain(conn, "SELECT * FROM jobs WHERE salary_monthly_max >= 200000 LIMIT 100")
    assert "idx_jobs_salary_monthly_max" in plan


def test_min_ats_subquery_indexed(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path, monkeypatch)
    plan = _explain(conn, "SELECT * FROM jobs WHERE EXISTS "
                          "(SELECT 1 FROM ats_scores a WHERE a.job_id = jobs.id "
                          "AND a.total >= 50) ORDER BY date_found DESC, id DESC LIMIT 50")
    # the per-row subquery must stay index-backed (outer scan is inherent).
    # SQLite's wording for this plan is not stable here — across runs on the same
    # interpreter it prints "EXISTS USING INDEX", "USING INDEX", or "EXISTS"
    # wedged in between — so assert the property, not the phrasing: the
    # subquery is reached by SEARCH and ats_scores is never a full SCAN.
    assert "SEARCH a" in plan
    assert "SCAN a" not in plan
