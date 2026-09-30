"""
X-C (F2): a job that matches a Remove keyword but clears the pay goal is kept,
and every hide says which word caused it.

The old rule hid on the first negative match and recorded nothing about why — a
₱60,000/mo job disappeared because it also mentioned 'crypto', and the table gave
you no way to find out.
"""

import pytest

from db.repos import jobs as job_repo
from db.repos import settings as settings_repo


@pytest.fixture
def conn(tmp_db):
    return tmp_db


def _job(conn, job_id, *, title, salary=None, monthly=None):
    job_repo.upsert_stub(conn, job_id=job_id, job_url=f"https://x/{job_id}",
                         title=title, salary=salary)
    if monthly is not None:
        conn.execute("UPDATE jobs SET salary_monthly_min=?, salary_monthly_max=? WHERE id=(SELECT id FROM jobs WHERE job_id=?)",
                     (monthly, monthly, job_id))
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def _row(conn, job_id):
    return conn.execute("SELECT status, filter_hidden, keyword_hit FROM jobs WHERE job_id = ?",
                        (job_id,)).fetchone()


def test_a_job_that_clears_the_goal_is_not_hidden(conn):
    from app.services.keywords import apply_keyword_filters
    _job(conn, 1, title="VA — crypto desk experience", monthly=60000)
    neg, pos, restored, rescued = apply_keyword_filters(conn, [], ["crypto"],
                                                        pay_goal_monthly=40000)
    assert (neg, pos, rescued) == (0, 0, 1)
    r = _row(conn, 1)
    assert r["status"] == "New" and r["filter_hidden"] == 0
    assert r["keyword_hit"].startswith("rescued:crypto"), r["keyword_hit"]


def test_below_the_goal_it_is_hidden_and_the_word_is_recorded(conn):
    from app.services.keywords import apply_keyword_filters
    _job(conn, 2, title="VA — crypto desk experience", monthly=12000)
    neg, _pos, _restored, rescued = apply_keyword_filters(conn, [], ["crypto"],
                                                          pay_goal_monthly=40000)
    assert (neg, rescued) == (1, 0)
    r = _row(conn, 2)
    assert r["status"] == "Hidden" and r["filter_hidden"] == 1
    assert r["keyword_hit"] == "negative:crypto"


def test_an_invented_month_cannot_rescue_a_job(conn):
    """$6/hour with no stated hours has no monthly figure (X-A) — so it cannot win."""
    from app.services.keywords import apply_keyword_filters
    row = _job(conn, 3, title="VA — crypto desk experience", salary="$6000/hour")
    assert row["salary_monthly_min"] is None
    apply_keyword_filters(conn, [], ["crypto"], pay_goal_monthly=4000)
    assert _row(conn, 3)["status"] == "Hidden"


def test_the_goal_is_stored_for_the_auto_run(conn):
    from app.services.keywords import apply_keyword_filters, stored_pay_goal
    settings_repo.set(conn, "pay_goal_monthly", "50000")
    assert stored_pay_goal(conn) == 50000
    _job(conn, 4, title="crypto writer", monthly=52000)
    # no explicit goal passed: the stored one is what the scheduler will use
    assert apply_keyword_filters(conn, [], ["crypto"])[3] == 1
    settings_repo.set(conn, "pay_goal_monthly", "0")
    assert stored_pay_goal(conn) == 0


def test_a_rescued_job_is_re_hidden_when_the_goal_drops(conn):
    from app.services.keywords import apply_keyword_filters
    _job(conn, 5, title="crypto writer", monthly=52000)
    apply_keyword_filters(conn, [], ["crypto"], pay_goal_monthly=40000)
    assert _row(conn, 5)["status"] == "New"
    apply_keyword_filters(conn, [], ["crypto"], pay_goal_monthly=60000)
    r = _row(conn, 5)
    assert r["status"] == "Hidden" and r["keyword_hit"] == "negative:crypto"


def test_a_job_no_rule_touched_has_no_hit(conn):
    from app.services.keywords import apply_keyword_filters
    _job(conn, 6, title="Data entry clerk", monthly=80000)
    apply_keyword_filters(conn, [], ["crypto"], pay_goal_monthly=40000)
    assert _row(conn, 6)["keyword_hit"] == ""


def test_api_reports_the_rescue_and_the_goal(client):
    r = client.post("/api/keywords/apply", json={
        "positive": [], "negative": ["crypto"], "pay_goal_monthly": 30000})
    assert r.status_code == 200
    body = r.json()
    assert body["rescued"] == 0 and body["pay_goal_monthly"] == 30000
    assert client.get("/api/keywords").json()["negative"] == ["crypto"]
    assert client.get("/api/keywords").json()["pay_goal_monthly"] == 30000
