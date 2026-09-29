"""
tests/test_w_c_signals.py — W-C: the signals stop lying (P1 fit/hygiene split,
P7 follow-ups coverage, B3/B4 landed with W-B).

P1 in one line: `total` added 40 points of resume hygiene that are identical for
every job, so "ATS >= 50" asked the wrong question and hid the table.
"""

import sqlite3

import db.connection as dbconn
import db.migrate as dbmigrate
import resumes.ats as ats
import resumes.schema as rschema
from db.repos import jobs as job_repo
from db.repos import settings as settings_repo

MASTER = {
    "basics": {"name": "Jeyson", "email": "j@x.com", "phone": "+63 917 000 0000",
               "summary": "VA with 5 years of ops work."},
    "skills": ["Python", "Email Marketing", "Bookkeeping"],
    "work": [{"role": "Ops VA", "company": "Acme", "start": "2021", "end": "2024",
              "bullets": ["Ran bookkeeping and email marketing"]}],
    "education": [{"school": "UP", "degree": "BS", "year": "2020"}],
}


def _job(**kw):
    base = {"title": "Python dev", "skills": ["Python"], "keywords": "python, django",
            "description": "Need python."}
    base.update(kw)
    return base


class TestFitHygieneSplit:
    def test_fit_is_the_job_dependent_half(self):
        text = ats.resume_to_text(MASTER)
        a = ats.score_resume(text, _job())
        b = ats.score_resume(text, _job(skills=["Bookkeeping", "Data Entry", "X"],
                                        keywords="notourkeywordzzz"))
        assert "fit" in a and "hygiene" in a
        assert b["fit"] < a["fit"], "a job the resume fits less scores lower on fit"
        assert a["fit"] == a["breakdown"]["skills"] + a["breakdown"]["keywords"]
        assert a["hygiene"] == a["breakdown"]["format"] + a["breakdown"]["completeness"]
        assert a["fit"] + a["hygiene"] == a["total"]

    def test_hygiene_is_the_constant_half(self):
        """The resume's shape doesn't change between jobs — that's the whole
        reason a single 100-point threshold was useless as a job filter."""
        text = ats.resume_to_text(MASTER)
        scores = [ats.score_resume(text, _job(title=t, skills=s))
                  for t, s in [("A", ["Python"]), ("B", ["Data Entry"]),
                                ("C", ["Python", "Data Entry"])]]
        assert len({s["hygiene"] for s in scores}) == 1
        assert len({s["fit"] for s in scores}) > 1

    def test_fit_is_zero_when_the_job_lists_nothing(self):
        s = ats.score_resume(ats.resume_to_text(MASTER), _job(skills=[], keywords=""))
        assert s["fit"] == 0
        assert s["total"] == s["hygiene"] > 0


def _api_scorer(masters, job):
    """fit = the number in the title; hygiene fixed at 30 (what a real master scores)."""
    n = int(job["title"].rsplit(" ", 1)[-1])
    return ("master", {"total": n + 30, "fit": n, "hygiene": 30,
                       "breakdown": {}, "matched_skills": [], "missing_skills": [],
                       "suggestions": []})


def _seed(conn, n):
    for i in range(1, n + 1):
        job_repo.upsert_stub(conn, job_id=i, job_url=f"https://x/{i}", title=f"Job {i}")


def test_min_fit_is_a_sql_level_filter(client, monkeypatch):
    """fit >= N over 60 is the honest floor: it keeps jobs the resume actually
    fits instead of jobs whose total clears a resume-shape constant."""
    monkeypatch.setattr(rschema, "best_profile_for_job", _api_scorer)
    conn = dbconn.get_conn()
    _seed(conn, 100)   # fit 1..100, total 31..130
    res = client.get("/api/jobs", params={"min_fit": 60, "per_page": 50})
    data = res.json()
    assert data["total"] == 41, "41 jobs have fit >= 60, and total says so"
    assert all(int(j["title"].rsplit(" ", 1)[-1]) >= 60 for j in data["items"])
    # the old total-based floor would have kept a different, larger set
    assert client.get("/api/jobs", params={"min_ats": 60}).json()["total"] > data["total"]


def test_rows_carry_the_score_they_are_filtered_on(client, monkeypatch):
    """P1's other half: the table filtered on ATS while showing no ATS column."""
    monkeypatch.setattr(rschema, "best_profile_for_job", _api_scorer)
    conn = dbconn.get_conn()
    _seed(conn, 3)
    items = client.get("/api/jobs").json()["items"]
    assert {i["ats_fit"] for i in items} == {1, 2, 3}
    assert all(i["ats_total"] == i["ats_fit"] + 30 for i in items)
    assert all(i["ats_profile"] == "master" for i in items)


def test_sort_by_ats_orders_on_fit(client, monkeypatch):
    monkeypatch.setattr(rschema, "best_profile_for_job", _api_scorer)
    conn = dbconn.get_conn()
    _seed(conn, 5)
    titles = [j["title"] for j in
              client.get("/api/jobs", params={"sort": "ats", "order": "desc"}).json()["items"]]
    assert titles == ["Job 5", "Job 4", "Job 3", "Job 2", "Job 1"]


def test_unscored_jobs_sink_when_sorting_by_ats(client, monkeypatch):
    """A job with no ats_scores row must not occupy the top of a fit sort."""
    monkeypatch.setattr(rschema, "best_profile_for_job", _api_scorer)
    conn = dbconn.get_conn()
    _seed(conn, 3)
    client.get("/api/jobs")                       # builds the cache
    conn.execute("DELETE FROM ats_scores WHERE job_id = 3")
    conn.commit()   # the app reads through its own connection
    titles = [j["title"] for j in
              client.get("/api/jobs", params={"sort": "ats", "order": "desc"}).json()["items"]]
    assert titles == ["Job 2", "Job 1", "Job 3"]


class TestFollowUpsCoverage:
    """P7: the due-count allowed-list forgot New and Offer, so the badge understated
    the pipeline it was supposed to be warning about."""

    def _due(self, conn):
        return job_repo.get_stats(conn)["follow_ups_due"]

    def test_new_and_offer_count(self, tmp_db):
        conn = tmp_db
        for i, status in enumerate(["New", "Interested", "Applied", "Interviewing",
                                    "Offer", "Hired", "Rejected", "Hidden"], start=1):
            rid, _ = job_repo.upsert_stub(conn, job_id=i, job_url=f"http://x/{i}",
                                          title=f"T{i}")
            job_repo.update_status(conn, rid, status)
            job_repo.update_follow_up(conn, rid, "2020-01-01")
        assert self._due(conn) == 5, "everything except Hired/Rejected/Hidden is due"

    def test_future_date_is_not_due(self, tmp_db):
        rid, _ = job_repo.upsert_stub(conn=tmp_db, job_id=9, job_url="http://x/9",
                                      title="Later")
        job_repo.update_status(tmp_db, rid, "Applied")
        job_repo.update_follow_up(tmp_db, rid, "2999-01-01")
        assert self._due(tmp_db) == 0


def test_v9_adds_fit_column_idempotently(tmp_path):
    legacy = sqlite3.connect(str(tmp_path / "v9.db"))
    legacy.execute("CREATE TABLE ats_scores (job_id INTEGER, profile TEXT, "
                   "total REAL, updated_at TEXT, PRIMARY KEY (job_id, profile))")
    legacy.execute("INSERT INTO ats_scores VALUES (1, 'master', 40, 'now')")
    legacy.commit()
    dbmigrate.MIGRATIONS[9](legacy)
    dbmigrate.MIGRATIONS[9](legacy)
    assert "fit" in {r[1] for r in legacy.execute("PRAGMA table_info(ats_scores)")}
    assert legacy.execute("SELECT fit FROM ats_scores WHERE job_id=1").fetchone()[0] == 0


def test_cache_key_carries_the_scoring_version(client, monkeypatch):
    """Bumping ATS_SCORING_VERSION is how a formula change invalidates every
    existing ats_scores table — without it, old DBs keep serving old numbers."""
    from app import server as srv
    conn = dbconn.get_conn()
    assert srv._ats_cache_key(conn).split(":")[0] == "v2"


def test_settings_next_run_is_recomputed_when_interval_changes(tmp_path, monkeypatch):
    """B14 tail: saving a new scrape interval used to leave next_run at the old
    schedule, so the app said 'next run in 4h' while it was set to hourly."""
    from app import scheduler
    dbpath = tmp_path / "sched2.db"
    monkeypatch.setattr(dbconn, "DB_PATH", str(dbpath))
    conn = dbconn.init_db(dbconn.get_conn())
    settings_repo.set(conn, "auto_run_interval_hours", "1")
    settings_repo.set(conn, "next_run", "0")   # long overdue
    conn.commit()
    scheduler.recompute_next_run(conn)
    import time
    nxt = float(settings_repo.get(conn, "next_run"))
    assert time.time() < nxt <= time.time() + 3600 + 5