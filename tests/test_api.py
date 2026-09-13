"""
tests/test_api.py — FastAPI endpoint tests using the TestClient.
"""

import sys
from pathlib import Path

import pytest

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: F401  (shared `client` fixture now lives in conftest.py — W2.1)


class TestStats:
    def test_empty(self, client):
        res = client.get("/api/stats")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["follow_ups_due"] == 0

    def test_follow_ups_due_counts_active_jobs_past_their_date(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://t/1", title="Due Applied")
        job_repo.update_status(conn, 1, "Applied")
        job_repo.update_follow_up(conn, 1, "2020-01-01")          # overdue → counts
        job_repo.upsert_stub(conn, job_id=2, job_url="http://t/2", title="Due Interview")
        job_repo.update_status(conn, 2, "Interviewing")
        job_repo.update_follow_up(conn, 2, "2099-01-01")          # future → no
        job_repo.upsert_stub(conn, job_id=3, job_url="http://t/3", title="Due Rejected")
        job_repo.update_status(conn, 3, "Rejected")
        job_repo.update_follow_up(conn, 3, "2020-01-01")          # closed status → no
        job_repo.upsert_stub(conn, job_id=4, job_url="http://t/4", title="No date")
        job_repo.update_status(conn, 4, "Applied")                # no follow_up → no
        conn.close()

        res = client.get("/api/stats")
        assert res.json()["follow_ups_due"] == 1


class TestJobsList:
    def test_empty(self, client):
        res = client.get("/api/jobs")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_with_jobs(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B")
        conn.close()

        res = client.get("/api/jobs")
        data = res.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_filter_by_status(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B")
        job_repo.update_status(conn, 1, "Applied")
        conn.close()

        res = client.get("/api/jobs?status=Applied")
        data = res.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "Job A"

    def test_skills_or_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Job A",
                             skills=["Video Editing", "Audio Editing"])
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Job B",
                             skills=["Quickbooks"])
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="Job C",
                             skills=["Marketing"])
        conn.close()

        res = client.get("/api/jobs?skills=Quickbooks,Audio%20Editing&per_page=500")
        data = res.json()
        # OR across the whole table, not just one page
        assert data["total"] == 2
        assert {j["job_id"] for j in data["items"]} == {1, 2}

    def test_sort_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="C job")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="A job")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="B job")
        conn.close()

        data = client.get("/api/jobs?sort=title&order=asc").json()
        assert [j["title"] for j in data["items"]] == ["A job", "B job", "C job"]

        data = client.get("/api/jobs?sort=title&order=desc").json()
        assert [j["title"] for j in data["items"]] == ["C job", "B job", "A job"]

        # unknown sort falls back to default ordering, no error
        data = client.get("/api/jobs?sort=nonexistent").json()
        assert data["total"] == 3

    def test_has_salary_param(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="A", salary="$500/month")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="B", salary="TBD")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://test.com/3", title="C")  # no salary
        conn.close()

        data = client.get("/api/jobs?has_salary=1").json()
        assert data["total"] == 1
        assert data["items"][0]["job_id"] == 1

        # default = all
        data = client.get("/api/jobs").json()
        assert data["total"] == 3


class TestKeywordApply:
    def _seed(self):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1",
                             title="Bookkeeper", company="Xero Co")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2",
                             title="Video Editor", company="Globex")
        conn.execute("UPDATE jobs SET description=? WHERE job_id=1",
                     ("We love Xero and Quickbooks.",))
        conn.execute("UPDATE jobs SET description=? WHERE job_id=2",
                     ("Cut videos all day.",))
        conn.commit()
        conn.close()

    def test_no_keywords_noop(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.status_code == 200
        assert res.json()["total_hidden"] == 0

    def test_negative_hides_matching(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.status_code == 200
        assert res.json()["hidden_by_negative"] == 1

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "Hidden"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "New"
        hist = conn.execute(
            "SELECT old_status FROM job_history WHERE new_status='Hidden' AND job_id=1"
        ).fetchall()
        assert len(hist) == 1 and hist[0][0] == "New"
        conn.close()

    def test_positive_hides_nonmatching_new(self, client):
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        assert res.status_code == 200
        assert res.json()["hidden_by_positive"] == 1  # only the video editor

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "New"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "Hidden"
        conn.close()

    def test_stub_filtered_on_available_fields(self, client):
        """Unenriched stubs (no description yet) are judged on the fields they
        DO have — a fresh harvest is filtered immediately, not only after
        enrichment catches up."""
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=9, job_url="http://test.com/9", title="Stub Only")
        job_repo.upsert_stub(conn, job_id=10, job_url="http://test.com/10",
                             title="Xero Bookkeeper", skills=["Accounting"])
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        assert res.status_code == 200
        assert res.json()["hidden_by_positive"] == 1  # only the description-less stub w/o 'xero'

        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=9").fetchone()[0] == "Hidden"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=10").fetchone()[0] == "New"
        conn.close()

    def test_reapply_after_removing_keywords_restores(self, client):
        """The user's exact scenario: filter hides a job, user changes their
        mind, removes the keyword, re-applies → job comes back."""
        self._seed()
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.json()["hidden_by_negative"] == 1

        # Remove the keyword and re-apply (no keywords at all now)
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.json()["restored"] == 1
        assert res.json()["total_hidden"] == 0

        from db.connection import get_conn
        conn = get_conn()
        row = conn.execute("SELECT status, filter_hidden FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "New" and row["filter_hidden"] == 0
        conn.close()

    def test_user_hidden_not_auto_restored(self, client):
        """Jobs the user hid by hand (filter_hidden=0) stay hidden."""
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        self._seed()
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=2").fetchone()[0]
        job_repo.update_status(conn, row_id, "Hidden")  # manual hide
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": []})
        # job 2 has no 'xero' — but it's manually hidden, so restore must not touch it
        assert res.json()["restored"] == 0

        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "Hidden"
        conn.close()

    def test_manual_restate_not_clobbered(self, client):
        """Filter hides a job → user sets it to 'Applied' by hand → a later
        apply with removed keywords must NOT revert it to 'New'."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})

        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=1").fetchone()[0]
        job_repo.update_status(conn, row_id, "Applied")
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": [], "negative": []})
        assert res.json()["restored"] == 0

        conn = get_conn()
        row = conn.execute("SELECT status, filter_hidden FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "Applied" and row["filter_hidden"] == 0
        conn.close()

    def test_rehide_saves_current_status(self, client):
        """A job the user re-statused can be hidden again by a new filter run;
        its CURRENT status is what gets saved for a future restore."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})

        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        row_id = conn.execute("SELECT id FROM jobs WHERE job_id=1").fetchone()[0]
        job_repo.update_status(conn, row_id, "Applied")
        conn.close()

        # New filter run: negative 'xero' still matches → hides again, saving 'Applied'
        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        assert res.json()["hidden_by_negative"] == 1

        conn = get_conn()
        row = conn.execute("SELECT status, pre_filter_status FROM jobs WHERE job_id=1").fetchone()
        assert row["status"] == "Hidden" and row["pre_filter_status"] == "Applied"
        conn.close()

    def test_restore_all_flag(self, client):
        """The 'Restore' button: brings back everything filter-hidden even if
        keywords still match."""
        self._seed()
        client.post("/api/keywords/apply", json={"positive": [], "negative": ["xero"]})
        res = client.post("/api/keywords/apply", json={"positive": ["xero"], "negative": ["xero"], "restore": True})
        assert res.json()["restored"] == 1
        assert res.json()["total_hidden"] == 0

        from db.connection import get_conn
        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "New"
        conn.close()


class TestPipelineSSE:
    def test_zero_new_jobs_emits_skip_line(self, client, monkeypatch):
        """A run that finds nothing must say so (not skip enrich silently)."""
        from app import server as srv
        from scraper.pipeline import PipelineEvent

        def fake_harvest(client_, **kw):
            yield PipelineEvent("summary", "Harvest done: 0 new / 40 seen",
                                {"new": 0, "seen": 40})

        monkeypatch.setattr(srv, "harvest", fake_harvest)
        res = client.post("/api/pipeline/run", json={
            "keyword": "x", "categories": [], "skills": [], "posted_since": None,
        })
        assert res.status_code == 200
        assert "No new jobs — skipping enrichment" in res.text
        assert "event: done" in res.text
        # no enrich events at all
        assert "event: enrich_done" not in res.text

    def test_enrich_error_payload_passes_through(self, client, monkeypatch):
        """Error events keep row_id + error + progress so the frontend can
        render a real error line instead of a fake '[ok] <id>'."""
        from app import server as srv
        from scraper.pipeline import PipelineEvent

        def fake_enrich(client_, jobs, workers=3):
            yield PipelineEvent("log", "Enriching 1 job(s) with 3 worker(s)…")
            yield PipelineEvent("enrich_result", "[1/1] err",
                                {"progress": "1/1", "row_id": 5, "error": "boom"})
            yield PipelineEvent("summary", "done",
                                {"open": 0, "closed": 0, "errors": 1, "total": 1})

        monkeypatch.setattr(srv, "enrich", fake_enrich)
        # seed a job so the check flow has something to re-check
        # (URL must be under the configured base_url — stray domains are skipped by design)
        from db.connection import get_conn
        from db.repos import jobs as job_repo
        c = get_conn()
        job_repo.upsert_stub(c, job_id=5, job_url="https://www.onlinejobs.ph/jobseekers/job/x-5", title="T")
        c.close()
        res = client.post("/api/pipeline/check", json={"workers": 2})
        assert res.status_code == 200
        import json as _json
        data_lines = [l for l in res.text.splitlines()
                      if l.startswith("data:") and '"error"' in l]
        payload = _json.loads(data_lines[0][len("data:"):].strip())
        assert payload["error"] == "boom"
        assert payload["progress"] == "1/1"
        assert payload["row_id"] == 5


class TestKeywordMatcher:
    """Whole-word semantics: 'AI' must not match inside other words."""

    def test_ai_whole_word(self):
        from app.server import _keyword_regexes
        [p] = _keyword_regexes(["AI"])
        for good in ("AI Data Annotator", "ai-powered video", "Ai.", "  ai  ", "AI models"):
            assert p.search(good.lower()), f"should match: {good!r}"
        for bad in ("Email Support VA", "chain of command", "maintenance", "certain", "emailing"):
            assert not p.search(bad.lower()), f"should NOT match: {bad!r}"

    def test_plural_and_multiword(self):
        from app.server import _keyword_regexes
        pcall, pflex = _keyword_regexes(["call", "flexible hours"])
        assert pcall.search("phone calls")          # simple plural ok
        assert not pcall.search("calling")          # not a different word
        assert pflex.search("FLEXIBLE HOURS")       # phrase, case-insensitive
        assert not pflex.search("flexiblehour")     # no boundary → no match

    def test_empty_keywords_ignored(self):
        from app.server import _keyword_regexes
        assert _keyword_regexes(["", "   "]) == []


class TestKeywordWholeWordAPI:
    """End-to-end through /api/keywords/apply with the user's actual scenario."""

    def test_positive_ai_does_not_match_email(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Email Support VA")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="AI Data Annotator")
        conn.execute("UPDATE jobs SET description=? WHERE job_id=1", ("Answer email, maintain records.",))
        conn.execute("UPDATE jobs SET description=? WHERE job_id=2", ("Train AI models.",))
        conn.commit()
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": ["AI"], "negative": []})
        assert res.status_code == 200
        assert res.json()["hidden_by_positive"] == 1  # only the non-AI job

        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "Hidden"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "New"
        conn.close()

    def test_negative_plural_video_catches_editors(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1", title="Short-form Video Editors")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2", title="Audio Engineer")
        conn.execute("UPDATE jobs SET description=? WHERE job_id=1", ("Edit TikTok videos.",))
        conn.execute("UPDATE jobs SET description=? WHERE job_id=2", ("Mix podcasts.",))
        conn.commit()
        conn.close()

        res = client.post("/api/keywords/apply", json={"positive": [], "negative": ["video"]})
        assert res.json()["hidden_by_negative"] == 1

        conn = get_conn()
        assert conn.execute("SELECT status FROM jobs WHERE job_id=1").fetchone()[0] == "Hidden"
        assert conn.execute("SELECT status FROM jobs WHERE job_id=2").fetchone()[0] == "New"
        conn.close()


class TestJobDetail:
    def test_found(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(
            conn, job_id=1, job_url="http://test.com/1",
            title="Test Job", company="Corp",
        )
        conn.close()

        res = client.get(f"/api/jobs/{row_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Test Job"
        assert "history" in data

    def test_not_found(self, client):
        res = client.get("/api/jobs/99999")
        assert res.status_code == 404


class TestStatusUpdate:
    def test_valid(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1")
        conn.close()

        res = client.patch(f"/api/jobs/{row_id}/status", json={"status": "Applied"})
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_invalid(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo

        conn = get_conn()
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://test.com/1")
        conn.close()

        res = client.patch(f"/api/jobs/{row_id}/status", json={"status": "Bogus"})
        assert res.status_code == 400


class TestSkills:
    def test_empty(self, client):
        res = client.get("/api/skills")
        assert res.status_code == 200
        assert res.json() == []


class TestIndex:
    def test_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "Job Hunter" in res.text


class TestPostedRangeAPI:
    """Date-posted column: sort matches the displayed date; range filters on the same key."""

    def _seed(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        ids = {}
        for n, posted, found in [
            (1, "2026-01-15 10:00:00", "2026-08-26 11:00:00"),
            (2, None, "2026-08-25 09:00:00"),      # no posted date → display falls back
            (3, "2026-01-05", "2026-08-26 10:00:00"),
        ]:
            rid, _ = job_repo.upsert_stub(conn, job_id=n, job_url=f"http://x/{n}", title=f"Job {n}")
            conn.execute("UPDATE jobs SET posted_date=?, date_found=? WHERE id=?", (posted, found, rid))
            ids[n] = rid
        conn.commit()
        conn.close()
        return ids

    def test_sort_asc_matches_displayed_date(self, client):
        self._seed(client)
        res = client.get("/api/jobs?sort=posted_date&order=asc&per_page=500")
        assert res.status_code == 200
        jobs = res.json()["items"]
        # displayed order: 2026-01-05, 2026-01-15, then the date_found-fallback row last
        assert [j["job_id"] for j in jobs] == [3, 1, 2]

    def test_sort_desc_reversed(self, client):
        self._seed(client)
        res = client.get("/api/jobs?sort=posted_date&order=desc&per_page=500")
        assert [j["job_id"] for j in res.json()["items"]] == [2, 1, 3]

    def test_range_filters(self, client):
        self._seed(client)
        assert client.get("/api/jobs?posted_from=2026-01-10").json()["total"] == 2
        assert client.get("/api/jobs?posted_to=2026-01-05").json()["total"] == 1
        assert client.get("/api/jobs?posted_from=2026-01-01&posted_to=2026-01-31").json()["total"] == 2
        assert client.get("/api/jobs?posted_from=2026-02-01").json()["total"] == 1


class TestCheckDomainGuard:
    def test_stray_domain_jobs_are_skipped(self, client, monkeypatch):
        from app import server as srv
        from scraper.pipeline import PipelineEvent

        seen = {}

        def fake_enrich(client_, jobs, workers=3):
            seen["jobs"] = list(jobs)
            yield PipelineEvent("log", "x")
            yield PipelineEvent("summary", "done",
                                {"open": 0, "closed": 0, "errors": 0, "total": len(jobs)})

        monkeypatch.setattr(srv, "enrich", fake_enrich)

        from db.connection import get_conn
        from db.repos import jobs as job_repo
        c = get_conn()
        good, _ = job_repo.upsert_stub(c, job_id=1, job_url="https://www.onlinejobs.ph/jobseekers/job/real-1")
        bad, _ = job_repo.upsert_stub(c, job_id=2, job_url="http://test.com/2")
        c.close()

        res = client.post("/api/pipeline/check", json={"workers": 1})
        assert res.status_code == 200
        assert [j[0] for j in seen["jobs"]] == [good]  # test.com never re-checked
        assert "test.com" not in res.text


class TestSchedule:
    def test_defaults(self, client):
        res = client.get("/api/schedule")
        assert res.status_code == 200
        data = res.json()
        assert data["enabled"] is True
        assert data["interval_hours"] == 4
        assert data["last_run"] == ""
        assert data["last_error"] == ""
        assert data["running"] is False

    def test_disable(self, client):
        res = client.post("/api/schedule", json={"enabled": False})
        assert res.status_code == 200
        assert client.get("/api/schedule").json()["enabled"] is False

    def test_interval(self, client):
        res = client.post("/api/schedule", json={"interval_hours": 2})
        assert res.status_code == 200
        assert client.get("/api/schedule").json()["interval_hours"] == 2

    def test_bad_interval_rejected(self, client):
        assert client.post("/api/schedule", json={"interval_hours": 0}).status_code == 400
        assert client.post("/api/schedule", json={"interval_hours": "x"}).status_code == 422
        # still 4
        assert client.get("/api/schedule").json()["interval_hours"] == 4


class TestHealth:
    def test_ok(self, client, monkeypatch):
        monkeypatch.setattr("app.server._probe_db", lambda: "ok")
        monkeypatch.setattr("app.server._probe_site", lambda: "ok")
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["db"] == "ok" and data["site"] == "ok"
        assert "pid" in data

    def test_site_unreachable_is_503_degraded(self, client, monkeypatch):
        monkeypatch.setattr("app.server._probe_db", lambda: "ok")
        monkeypatch.setattr("app.server._probe_site", lambda: "unreachable")
        res = client.get("/health")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded" and data["site"] == "unreachable"

    def test_db_locked_is_200_degraded(self, client, monkeypatch):
        monkeypatch.setattr("app.server._probe_db", lambda: "locked")
        monkeypatch.setattr("app.server._probe_site", lambda: "ok")
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "degraded" and data["db"] == "locked"

    def test_both_bad_site_wins_503(self, client, monkeypatch):
        monkeypatch.setattr("app.server._probe_db", lambda: "locked")
        monkeypatch.setattr("app.server._probe_site", lambda: "unreachable")
        res = client.get("/health")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["db"] == "locked" and data["site"] == "unreachable"


class TestHealthProbes:
    def test_probe_db_ok(self, client):
        import app.server as srv
        assert srv._probe_db() == "ok"          # temp test DB, fresh short conn

    def test_probe_db_locked(self, monkeypatch):
        import sqlite3 as _sq
        import app.server as srv

        def boom(path, **kw):
            raise _sq.OperationalError("database is locked")

        monkeypatch.setattr("app.server.sqlite3.connect", boom)
        assert srv._probe_db() == "locked"

    def test_probe_site_ok_and_unreachable(self, monkeypatch):
        import app.server as srv

        class R:
            status_code = 200

        monkeypatch.setattr("app.server.requests.get", lambda *a, **k: R())
        assert srv._probe_site("http://x/") == "ok"

        def down(*a, **k):
            raise srv.requests.RequestException("boom")

        monkeypatch.setattr("app.server.requests.get", down)
        assert srv._probe_site("http://x/") == "unreachable"


class TestCSVExport:
    def test_empty(self, client):
        res = client.get("/api/jobs/export")
        assert res.status_code == 200
        lines = res.text.strip().splitlines()
        assert "title" in lines[0]  # header row
        assert len(lines) == 1      # no data rows

    def test_with_jobs(self, client):
        from db.connection import get_conn
        from db.repos import jobs as job_repo
        conn = get_conn()
        job_repo.upsert_stub(conn, job_id=10, job_url="http://test.com/10", title="Export Me")
        conn.close()
        res = client.get("/api/jobs/export")
        assert res.status_code == 200
        lines = res.text.strip().splitlines()
        # header + data row
        assert len(lines) == 2
        assert "Export Me" in lines[1]


class TestEventsSSE:
    def test_stream_starts_with_connected(self, client):
        # TestClient's in-memory transport coalesces infinite streams, so
        # drive the endpoint's generator directly (uvicorn delivers each
        # send() as its own socket chunk to real EventSource clients).
        from app.server import events_stream
        resp = events_stream()
        import asyncio
        first = asyncio.run(resp.body_iterator.__anext__())
        asyncio.run(resp.body_iterator.aclose())
        assert "event: connected" in first

    def test_hub_publish_reaches_subscriber(self, client):
        from app import events as hub
        q = hub.subscribe()
        try:
            hub.publish("new_jobs", {"count": 2, "titles": ["A", "B"]})
            msg = q.get(timeout=1)
            assert "event: new_jobs" in msg
            assert "\"count\": 2" in msg
        finally:
            hub.unsubscribe(q)


@pytest.fixture
def conn(client):
    import db.connection as dbconn
    return dbconn.get_conn()


def test_keywords_apply_persists(client, conn):
    r = client.post("/api/keywords/apply",
                    json={"positive": ["remote"], "negative": ["crypto"]})
    assert r.status_code == 200
    b = client.get("/api/keywords").json()
    assert b["positive"] == ["remote"]
    assert b["negative"] == ["crypto"]


def test_scrape_scope_roundtrip(client):
    r = client.post("/api/scrape-scope",
                    json={"keyword": "VA, bookkeeping",
                          "categories": ["Virtual Assistant"],
                          "skills": ["Excel"]})
    assert r.status_code == 200
    b = client.get("/api/scrape-scope").json()
    assert b["keyword"] == "VA, bookkeeping"
    assert b["categories"] == ["Virtual Assistant"]
    assert b["skills"] == ["Excel"]


def test_reset_jobs_keeps_settings(client, conn):
    from db.repos import jobs as job_repo
    job_repo.upsert_stub(conn, job_id=1, job_url="http://r/1", title="A")
    job_repo.upsert_stub(conn, job_id=2, job_url="http://r/2", title="B")
    conn.commit()
    client.post("/api/keywords/apply", json={"positive": ["x"], "negative": ["y"]})
    client.post("/api/scrape-scope", json={"keyword": "zz"})
    r = client.post("/api/jobs/reset")
    assert r.status_code == 200
    assert r.json()["deleted_jobs"] == 2
    assert client.get("/api/jobs").json()["total"] == 0
    b = client.get("/api/keywords").json()
    assert b["positive"] == ["x"] and b["negative"] == ["y"]
    assert client.get("/api/scrape-scope").json()["keyword"] == "zz"
