"""W4.1: currency-aware salary.

v5 migration adds salary_currency + PHP-normalized salary_monthly_min/max
(FX rate from config `fx_to_php`), backfills existing salary text, and
records the rate used (quality bar: rate + timestamp stored together).
The write path stores the same structured values; sort=salary becomes
numeric with no-salary rows sinking to the bottom in both directions.
"""

import json
import sqlite3

import db.connection as dbconn
import db.migrate as dbmigrate
from db.repos import jobs as job_repo
from scraper.salary import normalize_to_php

DEFAULT_USD = 58.0  # config.json fx_to_php.usd


def _set_live_config(monkeypatch, cfg: dict):
    import app.config as appconfig
    # _live feeds the write path (jobs repo); _read_disk feeds the v5
    # backfill (it deliberately does not use the live cache)
    monkeypatch.setattr(appconfig, "_live", cfg)
    monkeypatch.setattr(appconfig, "_read_disk", lambda: cfg)


FIXTURE_ROWS = [
    # (job_url, salary text, v4-backfilled min/max)
    ("u1", "US$800/mo", 800.0, 800.0),
    ("u2", "₱60,000/mo", 60000.0, 60000.0),
    ("u3", "TBD", None, None),
]


def _make_db(tmp_path, version: int, legacy: bool):
    """A DB at `version` holding the fixture rows. `legacy` builds the
    pre-v5 table shape (no currency/monthly columns)."""
    path = tmp_path / "w41.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if legacy:
        conn.execute(
            """CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_url TEXT UNIQUE NOT NULL,
                title TEXT,
                salary TEXT,
                employer_id INTEGER,
                repost_of INTEGER,
                salary_min REAL,
                salary_max REAL,
                status TEXT DEFAULT 'New')"""
        )
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    else:
        conn.executescript(dbconn.SCHEMA)
    for url, sal, mn, mx in FIXTURE_ROWS:
        conn.execute(
            "INSERT INTO jobs (job_url, title, salary, salary_min, salary_max) "
            "VALUES (?, ?, ?, ?, ?)",
            (url, url, sal, mn, mx),
        )
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def _row(conn, url: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE job_url = ?", (url,)).fetchone()


# ── normalize_to_php ─────────────────────────────────────────────────────────


def test_normalize_to_php_basic():
    assert normalize_to_php(800.0, 1200.0, "USD") == (46400.0, 69600.0)
    assert normalize_to_php(800.0, 1200.0, "PHP") == (800.0, 1200.0)


def test_normalize_to_php_unknown_or_missing():
    # No rate, no guess: an unknown currency normalizes to NULL, never approx.
    assert normalize_to_php(800.0, 1200.0, "EUR") == (None, None)
    assert normalize_to_php(800.0, 1200.0, None) == (None, None)
    assert normalize_to_php(None, None, "USD") == (None, None)


def test_normalize_to_php_config_rate_wins():
    assert normalize_to_php(800.0, 1200.0, "USD", {"usd": 50.0}) == (40000.0, 60000.0)


# ── v5 migration ─────────────────────────────────────────────────────────────


def test_v5_backfill_legacy_db(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    conn = _make_db(tmp_path, 4, legacy=True)

    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbconn.SCHEMA_VERSION
    u1 = _row(conn, "u1")
    assert u1["salary_currency"] == "USD"
    assert u1["salary_monthly_min"] == 46400.0
    assert u1["salary_monthly_max"] == 46400.0
    u2 = _row(conn, "u2")
    assert u2["salary_currency"] == "PHP"
    assert u2["salary_monthly_min"] == 60000.0
    u3 = _row(conn, "u3")
    assert u3["salary_currency"] is None
    assert u3["salary_monthly_min"] is None
    # The rate used is recorded with a timestamp (quality bar)
    fx = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key = 'fx_metadata'").fetchone()["value"])
    assert fx["usd"] == DEFAULT_USD
    assert fx["at"]

    # raw values and text preserved
    assert u1["salary_min"] == 800.0
    assert u1["salary"] == "US$800/mo"


def test_v5_backfill_fresh_schema_db(tmp_path, monkeypatch):
    """A v4 DB that already has the final table shape: only the backfill runs."""
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    conn = _make_db(tmp_path, 4, legacy=False)
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbconn.SCHEMA_VERSION
    assert _row(conn, "u1")["salary_monthly_max"] == 46400.0
    assert _row(conn, "u2")["salary_monthly_max"] == 60000.0
    assert _row(conn, "u3")["salary_monthly_max"] is None


def test_v5_run_twice_is_stable(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    conn = _make_db(tmp_path, 4, legacy=True)
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    first = [dict(r) for r in conn.execute(
        "SELECT job_url, salary_currency, salary_monthly_min, salary_monthly_max "
        "FROM jobs ORDER BY id")]
    # version gate: a second run() is a no-op
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    # and the step itself is idempotent if forced to run again
    dbmigrate.MIGRATIONS[5](conn)
    conn.commit()
    second = [dict(r) for r in conn.execute(
        "SELECT job_url, salary_currency, salary_monthly_min, salary_monthly_max "
        "FROM jobs ORDER BY id")]
    assert first == second


def test_v5_records_fx_override(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": 50.0}})
    conn = _make_db(tmp_path, 4, legacy=True)
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    assert _row(conn, "u1")["salary_monthly_min"] == 40000.0
    fx = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key = 'fx_metadata'").fetchone()["value"])
    assert fx["usd"] == 50.0


def test_v5_metadata_keys_lowercased(tmp_path, monkeypatch):
    """A user config with uppercase 'USD' must not duplicate the default 'usd'."""
    _set_live_config(monkeypatch, {"fx_to_php": {"USD": 50.0}})
    conn = _make_db(tmp_path, 4, legacy=True)
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    assert _row(conn, "u1")["salary_monthly_min"] == 40000.0  # lookup is case-insensitive
    fx = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key = 'fx_metadata'").fetchone()["value"])
    assert fx == {"usd": 50.0, "at": fx["at"]}


def test_v5_no_parseable_rows_no_fx_metadata(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    conn = _make_db(tmp_path, 4, legacy=True)
    conn.execute("UPDATE jobs SET salary = 'TBD'")
    conn.commit()
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    assert conn.execute(
        "SELECT COUNT(*) FROM app_settings WHERE key = 'fx_metadata'"
    ).fetchone()[0] == 0  # W4.2 tooltip falls back to the config default


def test_fresh_db_reaches_v5(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "fresh.db"))
    conn = dbconn.init_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbconn.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"salary_currency", "salary_monthly_min", "salary_monthly_max"} <= cols


# ── numeric sort ─────────────────────────────────────────────────────────────


def _db_at_v5(tmp_path, monkeypatch, legacy=True):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    conn = _make_db(tmp_path, 4, legacy=legacy)
    dbmigrate.run(conn, dbconn.SCHEMA_VERSION)
    return conn


def test_sort_salary_desc_php_first_nulls_last(tmp_path, monkeypatch):
    conn = _db_at_v5(tmp_path, monkeypatch)
    rows, _ = job_repo.get_jobs(conn, sort="salary", order="desc")
    assert [r["job_url"] for r in rows] == ["u2", "u1", "u3"]


def test_sort_salary_asc_nulls_still_last(tmp_path, monkeypatch):
    conn = _db_at_v5(tmp_path, monkeypatch)
    rows, _ = job_repo.get_jobs(conn, sort="salary", order="asc")
    assert [r["job_url"] for r in rows] == ["u1", "u2", "u3"]


# ── write path ───────────────────────────────────────────────────────────────


def _fresh(tmp_path, monkeypatch):
    _set_live_config(monkeypatch, {"fx_to_php": {"usd": DEFAULT_USD}})
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "write.db"))
    return dbconn.init_db()


def test_upsert_stub_stores_structured_salary(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    rid, is_new = job_repo.upsert_stub(
        conn, job_id=1, job_url="https://x/job/data-engineer-1",
        title="Data  Engineer,  Jr.", salary="US$800/mo")
    assert is_new
    row = job_repo.get_job(conn, rid)
    assert row["salary_currency"] == "USD"
    assert row["salary_min"] == 800.0
    assert row["salary_monthly_min"] == 46400.0
    assert row["norm_title"] == "dataengineerjr"

    # update branch: new salary text re-computes the structured fields
    rid2, is_new2 = job_repo.upsert_stub(
        conn, job_id=1, job_url="https://x/job/data-engineer-1",
        title="Data  Engineer,  Jr.", salary="₱60,000/mo")
    assert not is_new2 and rid2 == rid
    row = job_repo.get_job(conn, rid)
    assert row["salary_currency"] == "PHP"
    assert row["salary_monthly_max"] == 60000.0


def test_upsert_stub_no_salary_leaves_structured_null(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    rid, _ = job_repo.upsert_stub(
        conn, job_id=2, job_url="https://x/job/a-2", title="A", salary="TBD")
    row = job_repo.get_job(conn, rid)
    assert row["salary_currency"] is None
    assert row["salary_monthly_min"] is None


def test_enrich_recomputes_salary(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    rid, _ = job_repo.upsert_stub(
        conn, job_id=3, job_url="https://x/job/b-3", title="B", salary="US$800/mo")
    job_repo.enrich_job(conn, rid, salary="US$1000/mo")
    row = job_repo.get_job(conn, rid)
    assert row["salary_monthly_min"] == 58000.0

    # salary=None is "not captured" — must not clobber
    job_repo.enrich_job(conn, rid, description="more")
    row = job_repo.get_job(conn, rid)
    assert row["salary_monthly_min"] == 58000.0


def test_enrich_same_unparseable_text_keeps_numbers(tmp_path, monkeypatch):
    """Re-check returns the same unparseable text again → keep what we had."""
    conn = _fresh(tmp_path, monkeypatch)
    rid, _ = job_repo.upsert_stub(
        conn, job_id=4, job_url="https://x/job/c-4", title="C", salary="US$800/mo")
    job_repo.enrich_job(conn, rid, salary="hello world")  # text changed, unparseable → clear
    row = job_repo.get_job(conn, rid)
    assert row["salary_monthly_min"] is None and row["salary"] == "hello world"
    job_repo.enrich_job(conn, rid, salary="hello world")  # same text again → keep (None) but no churn
    row = job_repo.get_job(conn, rid)
    assert row["salary_monthly_min"] is None


def test_upsert_same_unparseable_text_keeps_numbers(tmp_path, monkeypatch):
    """Stub re-seen with the same unparseable salary must not erase captured values."""
    conn = _fresh(tmp_path, monkeypatch)
    job_repo.upsert_stub(
        conn, job_id=5, job_url="https://x/job/d-5", title="D", salary="US$800/mo")
    job_repo.upsert_stub(
        conn, job_id=5, job_url="https://x/job/d-5", title="D", salary="US$800/mo")
    row = conn.execute("SELECT * FROM jobs WHERE job_url = 'https://x/job/d-5'").fetchone()
    assert row["salary_monthly_min"] == 46400.0


def test_normalize_to_php_non_numeric_rate(tmp_path, monkeypatch):
    """Config is a trust boundary: a hand-edited string rate is no rate, not a crash."""
    assert normalize_to_php(800.0, 1200.0, "USD", {"usd": "58"}) == (None, None)
    assert normalize_to_php(800.0, 1200.0, "USD", {"usd": None}) == (None, None)


# ── golden: 15 real salary strings from the live jobs.db (W4.1 verify) ──────

# (salary text as it appears in jobs.db, expected salary_monthly_min, _max) at
# fx usd=58.0. 'discussed'/'TBD' rows have no salary → NULL, never a guess.
GOLDEN_15 = [
    ("US$800/mo", 46400.0, 46400.0),
    ("₱60,000/mo", 60000.0, 60000.0),
    ("TBD", None, None),
    ("$5.00/hour", 46400.0, 46400.0),
    ("$450 usd per month", 26100.0, 26100.0),
    ("Need to be discussed", None, None),
    ("$425", 24650.0, 24650.0),
    ("$1200 USD a Month", 69600.0, 69600.0),
    ("$16/hour", 148480.0, 148480.0),
    ("1000 USD", 58000.0, 58000.0),
    ("30000 PHP Peso", 30000.0, 30000.0),
    ("47,000-170,000", 47000.0, 170000.0),
    ("$700-$1,000/month", 40600.0, 58000.0),
    ("Starting 40000 PHP", 40000.0, 40000.0),
    ("$1300-1700/month", 75400.0, 98600.0),
]


def test_golden_15_real_salary_strings(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    for i, (text, _, _) in enumerate(GOLDEN_15, start=1):
        job_repo.upsert_stub(
            conn, job_id=i, job_url=f"https://x/g/{i}",
            title=f"Golden {i}", salary=text)

    # per-row: stored normalization matches the hand-computed golden table,
    # raw text and raw min/max preserved
    for i, (text, pmin, pmax) in enumerate(GOLDEN_15, start=1):
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_url = ?", (f"https://x/g/{i}",)
        ).fetchone()
        assert row["salary"] == text, text
        assert row["salary_monthly_min"] == pmin, text
        assert row["salary_monthly_max"] == pmax, text

    # spec acceptance: mixed currencies sort by PHP value, NULLs last in both
    # directions ($700-$1k and $16/hr are 58000 — id tie-break keeps it stable)
    rows, _ = job_repo.get_jobs(conn, sort="salary", order="desc")
    assert [r["job_url"] for r in rows] == [
        "https://x/g/12",  # 170000
        "https://x/g/9",   # 148480
        "https://x/g/15",  # 98600
        "https://x/g/8",   # 69600
        "https://x/g/2",   # 60000
        "https://x/g/10",  # 58000 (id 10 < 13)
        "https://x/g/13",  # 58000
        "https://x/g/1",   # 46400
        "https://x/g/4",   # 46400
        "https://x/g/14",  # 40000
        "https://x/g/11",  # 30000
        "https://x/g/5",   # 26100
        "https://x/g/7",   # 24650
        "https://x/g/3",   # TBD
        "https://x/g/6",   # discussed
    ]
    rows, _ = job_repo.get_jobs(conn, sort="salary", order="asc")
    urls = [r["job_url"] for r in rows]
    assert urls[-2:] == ["https://x/g/3", "https://x/g/6"]  # NULLs still last
    assert urls[:-2] == ["https://x/g/7", "https://x/g/5", "https://x/g/11",
                         "https://x/g/14", "https://x/g/1", "https://x/g/4",
                         "https://x/g/10", "https://x/g/13", "https://x/g/2",
                         "https://x/g/8", "https://x/g/15", "https://x/g/9",
                         "https://x/g/12"]
