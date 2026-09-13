"""W5.3 golden tests — pagination contract on GET /api/jobs.

{items, page, per_page, total, next_cursor}; per_page capped at 500
(oversize → 400 envelope); next_cursor is null on the last page.
"""


def _seed(n: int):
    from db.connection import get_conn
    from db.repos import jobs as job_repo
    conn = get_conn()
    for i in range(1, n + 1):
        job_repo.upsert_stub(conn, job_id=i, job_url=f"http://t/{i}", title=f"Job {i}")


class TestPaginationContract:
    def test_shape_and_cursor(self, client):
        _seed(5)
        r = client.get("/api/jobs", params={"page": 1, "per_page": 2})
        assert r.status_code == 200
        data = r.json()
        assert set(data) == {"items", "page", "per_page", "total", "next_cursor"}
        assert len(data["items"]) == 2
        assert data["total"] == 5
        assert data["next_cursor"] == "2"  # more pages exist

        r = client.get("/api/jobs", params={"page": 3, "per_page": 2})
        data = r.json()
        assert len(data["items"]) == 1      # last page is short
        assert data["next_cursor"] is None  # nothing after page 3

    def test_oversize_per_page_rejected(self, client):
        _seed(1)
        r = client.get("/api/jobs", params={"per_page": 99999})
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "bad_request"
        assert "per_page" in body["error"]["message"]

    def test_page_below_one_clamps(self, client):
        _seed(2)
        r = client.get("/api/jobs", params={"page": 0})
        assert r.status_code == 200
        assert r.json()["page"] == 1
        assert len(r.json()["items"]) == 2

    def test_filters_still_apply_with_pagination(self, client):
        _seed(3)
        from db.connection import get_conn
        from db.repos import jobs as job_repo
        job_repo.update_status(get_conn(), 1, "Applied")
        r = client.get("/api/jobs", params={"status": "Applied", "per_page": 50})
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "Job 1"
