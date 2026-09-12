"""
tests/test_db.py — DB schema and repository tests.
"""

import pytest
import sqlite3

from db.connection import SCHEMA, SCHEMA_VERSION, init_db
from db.repos import jobs as job_repo
from db.repos import skills as skill_repo


@pytest.fixture
def conn():
    """In-memory DB with schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


class TestSchema:
    def test_jobs_table_exists(self, conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [r[0] for r in rows]
        assert "jobs" in names
        assert "job_history" in names
        assert "skill_tags" in names

    def test_jobs_columns(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        for expected in [
            "id", "job_id", "job_url", "title", "company", "description",
            "salary", "location", "hours_per_week", "work_type",
            "posted_date", "date_updated", "skills", "employer_id",
            "search_keyword", "search_category", "scrape_status", "scrape_reason",
            "status", "filter_hidden", "pre_filter_status", "date_applied", "notes", "follow_up",
            "date_found", "last_checked",
        ]:
            assert expected in cols, f"Missing column: {expected}"

    def test_unique_job_id(self, conn):
        conn.execute("INSERT INTO jobs (job_id, job_url) VALUES (1, 'http://a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (job_id, job_url) VALUES (1, 'http://b')")

    def test_unique_job_url(self, conn):
        conn.execute("INSERT INTO jobs (job_url) VALUES ('http://a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (job_url) VALUES ('http://a')")


class TestJobRepos:
    def test_upsert_stub_new(self, conn):
        row_id, is_new = job_repo.upsert_stub(
            conn, job_id=100, job_url="http://x.com/job/100",
            title="Test Job", work_type="Full Time",
        )
        assert is_new is True
        assert row_id > 0
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "Test Job"
        assert row["status"] == "New"

    def test_upsert_stub_existing(self, conn):
        job_repo.upsert_stub(conn, job_id=100, job_url="http://x.com/job/100", title="Old")
        row_id, is_new = job_repo.upsert_stub(
            conn, job_id=100, job_url="http://x.com/job/100", title="New",
        )
        assert is_new is False
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "New"

    def test_update_status(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        job_repo.update_status(conn, row_id, "Applied")
        row = job_repo.get_job(conn, row_id)
        assert row["status"] == "Applied"

    def test_update_status_invalid(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        with pytest.raises(ValueError):
            job_repo.update_status(conn, row_id, "Bogus")

    def test_status_history(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://x")
        job_repo.update_status(conn, row_id, "Applied")
        job_repo.update_status(conn, row_id, "Interviewing")
        history = job_repo.get_job_history(conn, row_id)
        assert len(history) == 2
        assert history[0]["new_status"] == "Interviewing"
        assert history[1]["new_status"] == "Applied"

    def test_get_stats(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Applied")
        stats = job_repo.get_stats(conn)
        assert stats["total"] == 2
        assert stats["New"] == 1
        assert stats["Applied"] == 1

    def test_get_jobs_filter_by_status(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Applied")

        rows, total = job_repo.get_jobs(conn, status="Applied")
        assert total == 1
        assert rows[0]["title"] == "A"

    def test_get_jobs_exclude_hidden(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        job_repo.update_status(conn, 1, "Hidden")

        rows, total = job_repo.get_jobs(conn)
        assert total == 1

        rows, total = job_repo.get_jobs(conn, include_hidden=True)
        assert total == 2

    def test_get_jobs_skills_or_filter(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A", skills=["Video Editing", "Audio Editing"])
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B", skills=["Accounting", "Quickbooks"])
        job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="C", skills=["Marketing"])

        # OR logic: matches job 1 (Audio) and job 2 (Quickbooks), not job 3
        rows, total = job_repo.get_jobs(conn, skills="Quickbooks, Audio Editing")
        assert total == 2
        assert {r["job_id"] for r in rows} == {1, 2}

        # single skill still works through the same param
        rows, total = job_repo.get_jobs(conn, skills="Xero")
        assert total == 0

    def test_get_jobs_work_type_multi(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A", work_type="Part Time")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B", work_type="Full Time")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="C", work_type="Gig")

        rows, total = job_repo.get_jobs(conn, work_type="Part Time, Gig")
        assert total == 2
        assert {r["job_id"] for r in rows} == {1, 3}

    def test_get_jobs_sort(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="C job", posted_date="2026-08-01 10:00:00")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="A job", posted_date="2026-08-03 10:00:00")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="B job", posted_date="2026-08-02 10:00:00")

        rows, _ = job_repo.get_jobs(conn, sort="title", order="asc")
        assert [r["title"] for r in rows] == ["A job", "B job", "C job"]

        rows, _ = job_repo.get_jobs(conn, sort="title", order="desc")
        assert [r["title"] for r in rows] == ["C job", "B job", "A job"]

        rows, _ = job_repo.get_jobs(conn, sort="posted_date", order="asc")
        assert [r["job_id"] for r in rows] == [1, 3, 2]

        # unknown sort falls back to newest-first default (all same date_found → id desc)
        rows, _ = job_repo.get_jobs(conn, sort="password")
        assert [r["job_id"] for r in rows] == [3, 2, 1]

    def test_get_jobs_text_filters(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="Bookkeeper Needed",
                             company="Acme Corp", salary="$500/month", location="Remote")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="Editor",
                             company="Globex", salary="$300/month", location="On-site")

        rows, total = job_repo.get_jobs(conn, company="acme")
        assert total == 1 and rows[0]["job_id"] == 1

        rows, total = job_repo.get_jobs(conn, salary="300")
        assert total == 1 and rows[0]["job_id"] == 2

        rows, total = job_repo.get_jobs(conn, location="Remote", company="acme")
        assert total == 1  # AND across different columns

    def test_get_jobs_has_salary(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A", salary="$500/month")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B", salary="TBD")
        job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="C", salary="Negotiable")
        job_repo.upsert_stub(conn, job_id=4, job_url="http://4", title="D")  # no salary at all

        rows, total = job_repo.get_jobs(conn, has_salary=True)
        assert total == 1
        assert rows[0]["job_id"] == 1

        # default returns everything
        rows, total = job_repo.get_jobs(conn)
        assert total == 4

    def test_get_jobs_scrape_status_multi(self, conn):
        r1, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="A")
        r2, _ = job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="B")
        conn.execute("UPDATE jobs SET scrape_status='Open' WHERE id=?", (r1,))
        conn.execute("UPDATE jobs SET scrape_status='Closed' WHERE id=?", (r2,))
        conn.commit()

        rows, total = job_repo.get_jobs(conn, scrape_status="Closed")
        assert total == 1 and rows[0]["job_id"] == 2

        rows, total = job_repo.get_jobs(conn, scrape_status="Open, Closed")
        assert total == 2

    def test_enrich_job(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1")
        job_repo.enrich_job(
            conn, row_id,
            title="Enriched Title",
            company="Some Corp",
            description="Full description here",
            skills=["Python", "Django"],
            employer_id=42,
        )
        row = job_repo.get_job(conn, row_id)
        assert row["title"] == "Enriched Title"
        assert row["company"] == "Some Corp"
        assert row["description"] == "Full description here"
        assert row["skills"] == "Python, Django"
        assert row["employer_id"] == 42
        assert row["scrape_status"] == "Open"

    def test_enrich_closed(self, conn):
        row_id, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1")
        job_repo.enrich_job(conn, row_id, is_closed=True, close_reason="404")
        row = job_repo.get_job(conn, row_id)
        assert row["scrape_status"] == "Closed"
        assert row["scrape_reason"] == "404"

    def test_get_jobs_posted_sort_matches_display(self, conn):
        """The cell shows posted_date, falling back to date_found when missing.
        Sorting must match that displayed value in both directions — otherwise
        rows with a missing posted date (showing a recent date_found) sit at the
        top of ASC and read as a broken sort."""
        r1, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="old")
        conn.execute("UPDATE jobs SET posted_date='2025-06-01', date_found='2026-08-26 11:00:00' WHERE id=?", (r1,))
        r2, _ = job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="no-posted-date")
        conn.execute("UPDATE jobs SET posted_date=NULL, date_found='2026-08-25 09:00:00' WHERE id=?", (r2,))
        r3, _ = job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="mid")
        conn.execute("UPDATE jobs SET posted_date='2026-01-01', date_found='2026-08-26 10:00:00' WHERE id=?", (r3,))
        conn.commit()

        rows, _ = job_repo.get_jobs(conn, sort="posted_date", order="asc")
        assert [r["job_id"] for r in rows] == [1, 3, 2]

        rows, _ = job_repo.get_jobs(conn, sort="posted_date", order="desc")
        assert [r["job_id"] for r in rows] == [2, 3, 1]

    def test_get_jobs_posted_range(self, conn):
        r1, _ = job_repo.upsert_stub(conn, job_id=1, job_url="http://1", title="a")
        conn.execute("UPDATE jobs SET posted_date='2026-01-15 10:00:00', date_found='2026-08-26 11:00:00' WHERE id=?", (r1,))
        r2, _ = job_repo.upsert_stub(conn, job_id=2, job_url="http://2", title="b")
        conn.execute("UPDATE jobs SET posted_date=NULL, date_found='2026-08-25 09:00:00' WHERE id=?", (r2,))
        r3, _ = job_repo.upsert_stub(conn, job_id=3, job_url="http://3", title="c")
        conn.execute("UPDATE jobs SET posted_date='2026-01-05', date_found='2026-08-26 10:00:00' WHERE id=?", (r3,))
        conn.commit()

        rows, _ = job_repo.get_jobs(conn, posted_from="2026-01-10")
        assert [r["job_id"] for r in rows] == [1, 2]  # date_found fallback counts

        # boundary inclusive: posted on exactly the To date is included
        rows, _ = job_repo.get_jobs(conn, posted_to="2026-01-05")
        assert [r["job_id"] for r in rows] == [3]

        rows, _ = job_repo.get_jobs(conn, posted_from="2026-01-01", posted_to="2026-01-31")
        assert sorted(r["job_id"] for r in rows) == [1, 3]

        rows, _ = job_repo.get_jobs(conn, posted_from="2026-02-01")
        assert [r["job_id"] for r in rows] == [2]

    def test_needing_enrichment_domain_guard(self, conn):
        job_repo.upsert_stub(conn, job_id=1, job_url="https://www.onlinejobs.ph/jobseekers/job/x-1")
        job_repo.upsert_stub(conn, job_id=2, job_url="http://test.com/2")

        rows = job_repo.get_jobs_needing_enrichment(
            conn, max_age_days=7, status_filter=["New"], base_url="https://www.onlinejobs.ph"
        )
        assert [r["job_url"] for r in rows] == ["https://www.onlinejobs.ph/jobseekers/job/x-1"]  # test.com never re-checked

        # no guard → both included (back-compat)
        rows = job_repo.get_jobs_needing_enrichment(conn, max_age_days=7, status_filter=["New"])
        assert [r["job_url"] for r in rows] == [
            "https://www.onlinejobs.ph/jobseekers/job/x-1", "http://test.com/2"
        ]


class TestMigrations:
    def test_init_db_is_idempotent_and_honest(self):
        """B1/B2 regression: repeated init_db() must NOT re-stamp
        scrape_status on never-checked jobs (old code ran the UPDATE on
        every request, turning 'unknown' into false 'Open')."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        job_repo.upsert_stub(conn, job_id=7, job_url="http://x/7", title="T")

        init_db(conn)  # first run: version bump + one-time migration
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # never checked ⇒ unknown (empty), never 'Open'
        assert conn.execute(
            "SELECT scrape_status FROM jobs WHERE job_id=7"
        ).fetchone()[0] in ("", None)

        init_db(conn)  # second run: pure no-op
        row = conn.execute("SELECT scrape_status FROM jobs WHERE job_id=7").fetchone()
        assert row[0] in ("", None)

    def test_enriched_rows_keep_their_status(self):
        """The v2 repair must only touch never-checked rows."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        rid, _ = job_repo.upsert_stub(conn, job_id=8, job_url="http://x/8", title="T")
        job_repo.enrich_job(conn, rid, description="d", is_closed=False)
        init_db(conn)
        assert conn.execute(
            "SELECT scrape_status FROM jobs WHERE job_id=8"
        ).fetchone()[0] == "Open"

    def test_old_db_repair_on_version_bump(self):
        """A pre-v2 file with the historical corruption (never-checked rows
        stamped 'Open') gets repaired exactly once on the version bump."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        # simulate the old code's corruption: never-checked but 'Open'
        conn.execute(
            "INSERT INTO jobs (job_id, job_url, status, scrape_status, last_checked) "
            "VALUES (1, 'http://old/1', 'New', 'Open', NULL)"
        )
        conn.execute(
            "INSERT INTO jobs (job_id, job_url, status, scrape_status, last_checked) "
            "VALUES (2, 'http://old/2', 'New', 'Open', '2026-08-01 10:00:00')"
        )
        conn.commit()

        init_db(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT scrape_status FROM jobs WHERE job_id=1"
        ).fetchone()[0] in ("", None)  # repaired → unknown
        assert conn.execute(
            "SELECT scrape_status FROM jobs WHERE job_id=2"
        ).fetchone()[0] == "Open"  # genuinely checked → kept


class TestConnectionLifecycle:
    """W2.1: one shared SQLite connection per (worker thread, db path),
    closed on shutdown — instead of a fresh sqlite3.connect per request."""

    @staticmethod
    def _is_closed(conn) -> bool:
        try:
            conn.execute("SELECT 1")
            return False
        except sqlite3.ProgrammingError:
            return True

    def test_get_db_reuses_one_connection_per_thread(self, tmp_path, monkeypatch):
        import db.connection as dbconn
        from app import deps
        db_path = str(tmp_path / "life.db")
        monkeypatch.setattr(dbconn, "DB_PATH", db_path)
        dbconn.init_db()

        c1 = deps.get_db()
        c2 = deps.get_db()
        assert c1 is c2  # same thread, same path → same connection
        c1.execute("INSERT INTO jobs (job_id, job_url) VALUES (1, 'http://a')")
        c1.commit()
        assert c2.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    def test_200_requests_keep_connection_count_flat(self, client, monkeypatch):
        import sqlite3 as _sqlite3
        from app import deps
        calls: list = []
        real_connect = _sqlite3.connect

        def counting(*a, **k):
            calls.append(1)
            return real_connect(*a, **k)

        monkeypatch.setattr(_sqlite3, "connect", counting)
        deps.close_all()  # start from a clean registry
        for _ in range(50):
            assert client.get("/api/stats").status_code == 200
        mid = len(calls)  # first request may open (init + shared) conns once
        for _ in range(150):
            assert client.get("/api/stats").status_code == 200
        assert len(calls) == mid  # 200 sequential requests → connection count flat

    def test_shutdown_closes_shared_connections(self, tmp_path, monkeypatch):
        import db.connection as dbconn
        from app import deps
        from app.server import app
        from fastapi.testclient import TestClient
        db_path = str(tmp_path / "shut.db")
        monkeypatch.setattr(dbconn, "DB_PATH", db_path)
        dbconn.init_db()
        conn = None
        with TestClient(app):  # lifespan shutdown runs on context exit
            conn = deps.get_db()
        assert self._is_closed(conn)

    def test_closed_connection_is_transparently_replaced(self, tmp_path, monkeypatch):
        import db.connection as dbconn
        from app import deps
        db_path = str(tmp_path / "stale.db")
        monkeypatch.setattr(dbconn, "DB_PATH", db_path)
        dbconn.init_db()

        c1 = deps.get_db()
        c1.close()  # closed out from under the registry (no .closed flag to notice)
        c2 = deps.get_db()
        assert c2 is not c1
        c2.execute("INSERT INTO jobs (job_id, job_url) VALUES (9, 'http://s')")  # works, not ProgrammingError

    def test_get_db_distinct_paths_distinct_conns_lru(self, tmp_path, monkeypatch):
        import db.connection as dbconn
        from app import deps
        p1 = str(tmp_path / "a.db")
        p2 = str(tmp_path / "b.db")
        monkeypatch.setattr(dbconn, "DB_PATH", p1)
        dbconn.init_db()
        dbconn.init_db(dbconn.get_conn(p2))
        c1 = deps.get_db()
        c2 = deps.get_db(p2)
        assert c1 is not c2
        assert self._is_closed(c1)  # LRU: a thread holds one live conn — switching paths closes the other


class TestSkillRepos:
    def test_upsert(self, conn):
        rows = [
            {"id": 1, "name": "Python", "parent_id": 10, "slug": "python", "category_path": "Software"},
            {"id": 2, "name": "Django", "parent_id": 11, "slug": "django", "category_path": "Software > Web"},
        ]
        count = skill_repo.upsert_skills(conn, rows)
        assert count == 2
        all_skills = skill_repo.get_all_skills(conn)
        assert len(all_skills) == 2

    def test_search(self, conn):
        skill_repo.upsert_skills(conn, [
            {"id": 1, "name": "Python", "parent_id": None, "slug": "python", "category_path": "Software"},
            {"id": 2, "name": "JavaScript", "parent_id": None, "slug": "javascript", "category_path": "Web"},
        ])
        results = skill_repo.search_skills(conn, "python")
        assert len(results) == 1
        assert results[0]["name"] == "Python"
