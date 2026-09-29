"""
tests/test_w_e_export.py — W-E: one view in, one view out (B11, B6, B7).

B11 was the loudest of them: the CSV dumped all 1,232 rows no matter what the
screen showed, while the code comment claimed the opposite.
"""

import csv
import io
import db.connection as dbconn
from db.repos import jobs as job_repo


def _seed(conn, n, status="New", title="Job", company="Acme", base=1000):
    ids = []
    for i in range(1, n + 1):
        rid, _ = job_repo.upsert_stub(conn, job_id=base + i, job_url=f"http://x/{base}-{i}",
                                      title=f"{title} {i}", company=company)
        job_repo.enrich_job(conn, rid, title=f"{title} {i}", company=company,
                            salary="$800/mo", hours_per_week="40 hours/week")
        # the normalized monthly value, as W4.1 would write it with a live FX rate
        conn.execute("UPDATE jobs SET salary_monthly_min = 47000, "
                     "salary_monthly_max = 47000, salary_currency = 'USD' "
                     "WHERE id = ?", (rid,))
        if status != "New":
            job_repo.update_status(conn, rid, status)
        ids.append(rid)
    return ids


def _rows(resp_text):
    # the export carries a UTF-8 BOM so Excel opens it — strip it for parsing
    return list(csv.DictReader(io.StringIO(resp_text.lstrip("﻿"))))


def test_export_honours_every_filter(client, monkeypatch):
    """B11: the CSV used to be `SELECT ... FROM jobs` with no WHERE clause."""
    conn = dbconn.get_conn()
    _seed(conn, 10, status="New", title="Bookkeeper")
    _seed(conn, 4, status="Applied", title="Python dev", base=2000)
    conn.commit()

    all_rows = _rows(client.get("/api/jobs/export").text)
    assert len(all_rows) == 14

    only_new = _rows(client.get("/api/jobs/export", params={"status": "New"}).text)
    assert len(only_new) == 10
    assert all(r["status"] == "New" for r in only_new)

    search = _rows(client.get("/api/jobs/export", params={"search": "python"}).text)
    assert len(search) == 4

    money = _rows(client.get("/api/jobs/export",
                             params={"salary_min_monthly": 40000}).text)
    assert len(money) == 14, "the stored PHP-normalized monthly clears the bar"
    assert _rows(client.get("/api/jobs/export",
                            params={"salary_min_monthly": 60000}).text) == []

    hours = _rows(client.get("/api/jobs/export", params={"hours": "40"}).text)
    assert len(hours) == 14


def test_export_carries_the_columns_the_table_shows(client):
    """B11: the table renders a PHP-normalized salary, a repost badge, hidden
    state and a Fit score — none of which were in the CSV."""
    conn = dbconn.get_conn()
    a, b = _seed(conn, 2, title="Reposted role")
    conn.execute("UPDATE jobs SET repost_of = ?, salary_monthly_min = 47000, "
                 "salary_monthly_max = 47000, salary_currency = 'USD', "
                 "search_category = 'accounting' WHERE id = ?", (a, b))
    conn.commit()
    rows = _rows(client.get("/api/jobs/export").text)
    assert len(rows) == 2
    wide = next(r for r in rows if r["id"] == str(b))  # noqa: E501
    for col in ("salary_monthly_min", "salary_monthly_max", "salary_currency",
                "repost_of", "filter_hidden", "search_category", "ats_fit",
                "ats_total", "scrape_status", "pre_filter_status"):
        assert col in wide, f"CSV is missing {col}"
    assert wide["repost_of"] == str(a)
    assert wide["search_category"] == "accounting"


def test_export_is_utf8_with_a_bom_for_excel(client):
    body = client.get("/api/jobs/export").content
    assert body.startswith(b"\xef\xbb\xbf"), "Excel needs the BOM to read UTF-8"


def test_export_matches_the_page_it_was_called_from(client):
    """The acceptance rule: the same params give the same rows on both ends."""
    conn = dbconn.get_conn()
    _seed(conn, 12, title="VA")
    conn.commit()
    params = {"search": "VA", "status": "New", "per_page": 5, "page": 2}
    page = client.get("/api/jobs", params=params).json()
    exported = _rows(client.get("/api/jobs/export", params=params).text)
    assert len(exported) == page["total"] == 12, "export is the whole view, not one page"


def test_export_hides_reposts_when_told_to(client):
    conn = dbconn.get_conn()
    a, b = _seed(conn, 2, title="Same role")
    conn.execute("UPDATE jobs SET repost_of = ? WHERE id = ?", (a, b))
    conn.commit()
    assert len(_rows(client.get("/api/jobs/export").text)) == 2
    assert len(_rows(client.get("/api/jobs/export",
                               params={"hide_reposts": 1}).text)) == 1


def test_notes_and_follow_up_404_on_unknown_id(client):
    """B6: /status checked for existence, these two didn't — a typo'd id got 200
    and the drawer said "saved" over a row that isn't there."""
    assert client.patch("/api/jobs/999999/notes",
                        json={"notes": "x"}).status_code == 404
    assert client.patch("/api/jobs/999999/follow-up",
                        json={"follow_up": "2026-01-01"}).status_code == 404
    assert client.patch("/api/jobs/999999/status",
                        json={"status": "Applied"}).status_code == 404


def test_notes_and_follow_up_still_work_on_a_real_row(client):
    conn = dbconn.get_conn()
    rid, _ = job_repo.upsert_stub(conn, job_id=4242, job_url="http://x/4242", title="Real")
    conn.commit()
    assert client.patch(f"/api/jobs/{rid}/notes",
                        json={"notes": "called"}).json() == {"ok": True}
    assert client.patch(f"/api/jobs/{rid}/follow-up",
                        json={"follow_up": "2026-02-02"}).json() == {"ok": True}


def test_manual_check_stale_window_comes_from_config(client, monkeypatch):
    """B7: the manual "Check for updates" hardcoded 7 days while auto-run obeyed
    config's enrich_interval_days — one button, two definitions of stale."""
    from app import server as srv
    conn = dbconn.get_conn()
    for i in range(1, 6):
        rid, _ = job_repo.upsert_stub(conn, job_id=2000 + i,
                                      job_url=f"http://x/job/{i}", title=f"Old {i}")
        conn.execute("UPDATE jobs SET date_updated = datetime('now', '-20 days') "
                     "WHERE id = ?", (rid,))
    conn.commit()
    monkeypatch.setattr(srv, "_cfg", {"enrich_interval_days": 60})

    # An offline stand-in: every detail page reads as "gone", so the check
    # finishes without touching the network.
    from tests.test_parsers import GONE_HTML
    class Dead:
        stopped = False
        base_url = "http://x"
        def set_stop(self, token): self.stopped = token.stopped
        def get(self, url):
            from tests.test_pipeline import FakeResp
            return FakeResp(GONE_HTML)
    monkeypatch.setattr(srv, "get_client", lambda: Dead())
    captured = {}
    real = job_repo.get_jobs_needing_enrichment
    spy = lambda c, **kw: captured.update(kw) or real(c, **kw)
    monkeypatch.setattr(job_repo, "get_jobs_needing_enrichment", spy)

    with client:
        r = client.stream("POST", "/api/pipeline/check", json={"workers": 1})
        with r:
            pass  # the stream runs and finishes with no jobs to fetch
    assert captured.get("max_age_days") == 60, "config's window, not a hardcoded 7"


def test_explicit_max_age_days_still_wins(client, monkeypatch):
    from app import server as srv
    from tests.test_parsers import GONE_HTML
    from tests.test_pipeline import FakeResp
    class Dead:
        stopped = False
        base_url = "http://x"
        def set_stop(self, token): self.stopped = token.stopped
        def get(self, url): return FakeResp(GONE_HTML)
    monkeypatch.setattr(srv, "get_client", lambda: Dead())
    monkeypatch.setattr(srv, "_cfg", {"enrich_interval_days": 60})
    captured = {}
    real = job_repo.get_jobs_needing_enrichment
    monkeypatch.setattr(job_repo, "get_jobs_needing_enrichment",
                        lambda c, **kw: captured.update(kw) or real(c, **kw))
    with client.stream("POST", "/api/pipeline/check",
                       json={"workers": 1, "max_age_days": 3}):
        pass
    assert captured["max_age_days"] == 3