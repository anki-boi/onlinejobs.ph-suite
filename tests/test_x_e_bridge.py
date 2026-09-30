"""
X-E: the bridge to the browser extension (ojph-cleaner).

Two things the extension needs from the dashboard — a resume-fit score for a listing
it is looking at, and a link that opens a saved job — and one thing it must not get:
open access to a local dashboard holding someone's job history.
"""

import pytest


@pytest.fixture
def conn(tmp_db):
    return tmp_db


def test_fit_scores_a_listing_the_dashboard_never_saved(client):
    r = client.post("/api/resume/fit", json={"jobs": [
        {"title": "Executive Assistant", "description": "calendar management, email triage, "
         "data entry, CRM updates", "salary": "$800/mo"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    res = body["results"][0]
    assert res["profile"], "a profile was chosen"
    assert 0 <= res["fit"] <= 60 and 0 <= res["total"] <= 100
    # fit is the job-dependent half; hygiene is the resume's own formatting score
    assert res["total"] == res["fit"] + res["hygiene"]


def test_fit_is_per_listing_because_the_best_profile_is_per_listing(client):
    r = client.post("/api/resume/fit", json={"jobs": [
        {"title": "Video Editor", "description": "Premiere Pro, After Effects, reels"},
        {"title": "Data Entry Clerk", "description": "typing, spreadsheets, accuracy"}]})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 2


def test_fit_refuses_an_empty_batch_or_too_many(client):
    assert client.post("/api/resume/fit", json={"jobs": []}).status_code == 400
    assert client.post("/api/resume/fit",
                       json={"jobs": [{"title": "x"}] * 51}).status_code == 400


def test_the_deep_link_filter_finds_a_job_by_its_site_id(client):
    from db.connection import get_conn
    from db.repos import jobs as job_repo
    conn = get_conn()
    job_repo.upsert_stub(conn, job_id=990001, job_url="https://onlinejobs.ph/job/990001",
                         title="Deep linked job")
    items = client.get("/api/jobs?job_id=990001").json()["items"]
    assert [j["title"] for j in items] == ["Deep linked job"]
    assert client.get("/api/jobs?job_id=990002").json()["items"] == []


def test_only_the_site_the_bridge_is_for_can_read_it(client):
    ok = client.get("/api/stats", headers={"Origin": "https://www.onlinejobs.ph"})
    assert ok.headers.get("access-control-allow-origin") == "https://www.onlinejobs.ph"

    bad = client.get("/api/stats", headers={"Origin": "https://evil.example"})
    assert bad.headers.get("access-control-allow-origin") is None, (
        "a localhost dashboard is not a public API — '*' would let any tab read it")

    pre = client.options("/api/resume/fit", headers={"Origin": "https://onlinejobs.ph"})
    assert pre.status_code in (200, 204)
    assert pre.headers.get("access-control-allow-origin") == "https://onlinejobs.ph"


def test_the_allowlist_is_configurable(client, monkeypatch):
    from app import config as appconfig
    cfg = dict(appconfig.get())
    cfg["cors_origins"] = "https://dashboard.local"
    monkeypatch.setattr(appconfig, "get", lambda: cfg)
    assert client.get("/api/stats", headers={
        "Origin": "https://dashboard.local"}).headers.get("access-control-allow-origin") \
        == "https://dashboard.local"
    assert client.get("/api/stats", headers={
        "Origin": "https://www.onlinejobs.ph"}).headers.get("access-control-allow-origin") is None
