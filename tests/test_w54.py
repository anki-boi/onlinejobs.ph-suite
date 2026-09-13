"""W5.4 golden tests — ETag/304 on GET /api/jobs and idempotent run POSTs.

ETag is derived from the jobs-table version + the exact query string:
repeat GET with If-None-Match → 304 (no body) until data or query changes.
POST /api/pipeline/run with a scope matching the active run returns that
run's run_id (already_running) instead of a busy error.
"""


def _seed(n: int):
    from db.connection import get_conn
    from db.repos import jobs as job_repo
    conn = get_conn()
    for i in range(1, n + 1):
        job_repo.upsert_stub(conn, job_id=i, job_url=f"http://t/{i}", title=f"Job {i}")


class TestEtag:
    def test_repeat_get_returns_304(self, client):
        _seed(2)
        r1 = client.get("/api/jobs", params={"per_page": 50})
        assert r1.status_code == 200
        etag = r1.headers["ETag"]
        assert etag
        assert len(r1.json()["items"]) == 2

        r2 = client.get("/api/jobs", params={"per_page": 50},
                        headers={"If-None-Match": etag})
        assert r2.status_code == 304
        assert r2.content == b""  # 304 carries no body
        assert r2.headers["ETag"] == etag

    def test_data_change_invalidates_etag(self, client):
        _seed(2)
        r1 = client.get("/api/jobs", params={"per_page": 50})
        etag = r1.headers["ETag"]

        _seed(3)  # new row bumps the jobs version
        r2 = client.get("/api/jobs", params={"per_page": 50},
                        headers={"If-None-Match": etag})
        assert r2.status_code == 200          # version changed → full body again
        assert r2.headers["ETag"] != etag
        assert len(r2.json()["items"]) == 3

    def test_query_change_invalidates_etag(self, client):
        _seed(2)
        r1 = client.get("/api/jobs", params={"per_page": 50})
        etag = r1.headers["ETag"]
        r2 = client.get("/api/jobs", params={"per_page": 50, "status": "Applied"},
                        headers={"If-None-Match": etag})
        assert r2.status_code == 200          # different query → different ETag
        assert r2.headers["ETag"] != etag


class TestIdempotentRun:
    def _sse_lines(self, r):
        out = []
        for line in r.text.splitlines():
            if line.startswith("data:"):
                out.append(line[5:].strip())
        return out

    def test_identical_run_attaches_to_active(self, client, monkeypatch):
        from app import scheduler
        scope = {"keyword": "react", "categories": ["IT"], "skills": [], "posted_since": None}
        monkeypatch.setattr(scheduler, "is_running", lambda: True)
        monkeypatch.setattr(scheduler, "active_run_id", "abc123")
        monkeypatch.setattr(scheduler, "active_run_scope", scope)

        body = {"keyword": "react", "categories": ["IT"]}
        r = client.post("/api/pipeline/run", json=body)
        assert r.status_code == 200
        lines = self._sse_lines(r)
        assert '{"run_id": "abc123", "status": "already_running"}' in lines
        assert lines[-1] == 'already_running'

    def test_different_scope_still_busy(self, client, monkeypatch):
        import threading
        from app import scheduler
        scope = {"keyword": "react", "categories": ["IT"], "skills": [], "posted_since": None}
        held = threading.Lock()
        held.acquire()
        monkeypatch.setattr(scheduler, "is_running", lambda: True)
        monkeypatch.setattr(scheduler, "active_run_id", "abc123")
        monkeypatch.setattr(scheduler, "active_run_scope", scope)
        monkeypatch.setattr(scheduler, "pipeline_lock", held)

        r = client.post("/api/pipeline/run", json={"keyword": "vue"})
        assert r.status_code == 200
        lines = self._sse_lines(r)
        assert lines[-1] == 'busy'  # different scope → normal busy, not idempotent
