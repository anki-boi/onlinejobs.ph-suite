"""
X-A: what the salary columns are allowed to claim.

The rule the DB enforces: `salary_monthly_*` may only hold a figure the listing
supports. `salary_rate_*` holds the posted rate converted to PHP per unit, which
stays honest for a day rate or a piece rate. `salary_assumed_currency` marks the
one kind of guess that is allowed (a currency read off magnitude) so it is visible
instead of silent.
"""

import pytest

from db.repos import jobs as job_repo

FX = {"USD": 58.0, "AUD": 39.0, "CAD": 42.0}


@pytest.fixture
def conn(tmp_db):
    return tmp_db


def _fx(monkeypatch, rates=FX):
    import scraper.salary as salary
    monkeypatch.setattr(salary, "fetch_fx_to_php",
                        lambda currencies=None, timeout=10: dict(rates))


def _put(conn, job_id, salary, *, work_type=None, hours=None, title="Role"):
    job_repo.upsert_stub(conn, job_id=job_id, job_url=f"https://x/{job_id}",
                         title=title, work_type=work_type, hours=hours, salary=salary)
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def test_hourly_without_hours_gets_a_rate_not_a_month(conn, monkeypatch):
    _fx(monkeypatch)
    r = _put(conn, 1, "$16/hour")
    assert (r["salary_min"], r["salary_max"]) == (16.0, 16.0)   # posted figures
    assert r["salary_currency"] == "USD"
    assert r["salary_unit"] == "hour"
    assert r["salary_monthly_min"] is None                       # no invented month
    assert (r["salary_rate_min"], r["salary_rate_max"]) == (928.0, 928.0)  # ₱ per hour
    assert r["salary_hours_basis"] == "unstated"


def test_stated_hours_and_full_time_are_labelled_differently(conn, monkeypatch):
    _fx(monkeypatch)
    stated = _put(conn, 2, "$16/hour", hours="30 hours/week")
    assumed = _put(conn, 3, "$16/hour", work_type="Full Time")
    # a 4-week month, the same convention the extension's parser uses
    assert stated["salary_monthly_min"] == 16 * 30 * 4 * 58
    assert stated["salary_hours_basis"] == "stated"
    assert assumed["salary_monthly_min"] == 16 * 40 * 4 * 58
    assert assumed["salary_hours_basis"] == "full-time"          # an assumption, labelled
    assert assumed["salary_hours"] == 40


def test_part_time_prose_does_not_buy_a_full_time_month(conn, monkeypatch):
    _fx(monkeypatch)
    r = _put(conn, 4, "$6/hour", work_type="Part Time — some full time availability needed")
    assert r["salary_monthly_min"] is None
    assert r["salary_hours_basis"] == "part-time"


def test_piece_rate_is_a_rate_not_a_month(conn, monkeypatch):
    _fx(monkeypatch)
    r = _put(conn, 5, "$50-$150 per video", work_type="Full Time")
    assert r["salary_piece_rate"] == 1
    assert r["salary_monthly_min"] is None                       # even labelled full time
    assert (r["salary_rate_min"], r["salary_rate_max"]) == (2900.0, 8700.0)  # ₱ per video


def test_foreign_currency_codes_are_not_php_or_usd(conn, monkeypatch):
    _fx(monkeypatch, {"AUD": 39.0, "CAD": 42.0})
    assert _put(conn, 6, "AUD 15/hr")["salary_currency"] == "AUD"
    assert _put(conn, 7, "$5-7/hr CAD")["salary_currency"] == "CAD"
    assert _put(conn, 8, "3000 SGD per month")["salary_currency"] == "SGD"
    # SGD has no rate here: the posted figure is kept, the month is not invented
    r = conn.execute("SELECT * FROM jobs WHERE job_id = 8").fetchone()
    assert (r["salary_min"], r["salary_max"]) == (3000.0, 3000.0)
    assert r["salary_monthly_min"] is None


def test_a_currency_guessed_from_magnitude_is_marked(conn, monkeypatch):
    _fx(monkeypatch)
    bare = _put(conn, 9, "42000")
    stated = _put(conn, 10, "Php 42000")
    dollars = _put(conn, 11, "$400")
    assert (bare["salary_currency"], bare["salary_assumed_currency"]) == ("PHP", 1)
    assert (stated["salary_currency"], stated["salary_assumed_currency"]) == ("PHP", 0)
    assert (dollars["salary_currency"], dollars["salary_assumed_currency"]) == ("USD", 0)


def test_recompute_salaries_repairs_stale_rows(conn, monkeypatch):
    """The live damage: rows written by the old parser, repaired from raw text."""
    conn.execute("INSERT INTO jobs (job_id, job_url, title, salary, salary_currency, "
                 "salary_min, salary_max, salary_monthly_min, salary_monthly_max) "
                 "VALUES (12, 'https://x/12', 'Videographer', 'AUD 15/hr', 'PHP', "
                 "15, 15, 2400, 2400)")
    conn.commit()
    _fx(monkeypatch, {"AUD": 39.0})
    out = job_repo.recompute_salaries(conn)
    assert out["rows"] == 1
    r = conn.execute("SELECT * FROM jobs WHERE job_id = 12").fetchone()
    assert r["salary_currency"] == "AUD"
    assert r["salary_monthly_min"] is None            # ₱2,400/mo was a fiction
    assert r["salary_rate_min"] == 585.0              # ₱585 per hour is true
    assert out["rates"] == {"AUD": 39.0}


def test_no_live_rate_keeps_previous_money(conn, monkeypatch):
    """Offline: the currency/unit facts update, the PHP money is left alone."""
    _fx(monkeypatch, {"USD": 58.0})
    _put(conn, 1, "$16/hour")
    job_repo.recompute_salaries(conn)                 # sets ₱ figures with a rate
    _fx(monkeypatch, {})                              # now offline
    job_repo.recompute_salaries(conn)
    r = conn.execute("SELECT * FROM jobs WHERE job_id = 1").fetchone()
    assert r["salary_currency"] == "USD"
    assert r["salary_min"] == 16.0
    assert r["salary_rate_min"] == 928.0              # not blanked by an offline pass


def test_export_carries_the_new_columns(client):
    """The CSV must not silently drop the columns the table shows."""
    import csv
    import io
    body = client.get("/api/jobs/export").text
    cols = next(csv.reader(io.StringIO(body)))
    for col in ("salary_unit", "salary_hours_basis", "salary_rate_min",
                "salary_assumed_currency", "salary_piece_rate"):
        assert col in cols, f"{col} missing from the CSV"
