"""Tests for the /api/resume/build (1-page Harvard CV) endpoints."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf
from resumes import digest as resume_digest, yamlcv as resume_yamlcv

from fastapi.testclient import TestClient
from app.server import app


def _make_pdf(path: Path, pages: int = 1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "test.db"))
    dbconn.init_db()
    from app import server as srv
    monkeypatch.setattr(srv, "_client", None)
    # Personal-file isolation
    masters = tmp_path / "masters.json"
    masters.write_text('{"default": "master", "profiles": {"master": {"name": "Jeyson Dagondon", "email": "a@b.com"}}}')
    monkeypatch.setattr(srv, "MASTERS_PATH", masters)
    built = tmp_path / "built"
    monkeypatch.setattr(srv, "BUILT_DIR", built)
    monkeypatch.setattr(srv, "BASE_RESUMES", tmp_path / "resumes")
    monkeypatch.setattr(srv, "_cfg", {
        "llm_base_url": "http://x/v1", "llm_api_key": "k", "llm_model": "m",
        "resume_sources": [str(tmp_path)],
    })
    with TestClient(app) as c:
        yield c


def _seed_job(client):
    from db.connection import get_conn
    from db.repos import jobs as job_repo
    conn = get_conn()
    job_repo.upsert_stub(conn, job_id=7, job_url="http://t/7",
                         title="AI Automation & Business Process Specialist")
    conn.commit()
    conn.close()


def _fake_build(llm, corpus_text, identity, job_text, out_dir, max_rounds=4):
    (out_dir / "rendercv_output").mkdir(parents=True, exist_ok=True)
    p = out_dir / "rendercv_output" / "cv.pdf"
    _make_pdf(p, 1)
    return {"ok": True, "pages": 1, "rounds": 1, "yaml": "cv:\n  name: J\n",
            "pdf": str(p), "history": [{"round": 1, "ok": True, "pages": 1, "pdf": str(p)}]}


def test_build_success_lists_and_serves(client, monkeypatch, tmp_path):
    _seed_job(client)
    from app import server as srv
    monkeypatch.setattr(resume_yamlcv, "available", lambda: True)
    monkeypatch.setattr(resume_digest, "digest",
                        lambda s, **kw: {"sources": [{"file": "x", "chars": 100, "text": "t"}],
                                         "bullets": ["b"], "terms": ["sql"],
                                         "images": [], "chars": 500})
    monkeypatch.setattr(resume_digest, "corpus_text", lambda c, **kw: "CORPUS")
    monkeypatch.setattr(srv.LLMClient, "from_config",
                        classmethod(lambda cls, cfg: object()))
    monkeypatch.setattr(resume_yamlcv, "build_one_pager", _fake_build)

    r = client.post("/api/resume/build", json={"job_id": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["profile"] == "master"
    assert body["name"].startswith("AI_Automation") and body["name"].endswith(".pdf")

    lst = client.get("/api/resume/built").json()
    assert len(lst["items"]) == 1 and lst["items"][0]["name"] == body["name"]

    pdf = client.get(f"/api/resume/built/{body['name']}")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    # yaml sidecar saved
    y = srv.BUILT_DIR / body["name"].replace(".pdf", ".yaml")
    assert y.exists()


def test_build_503_without_toolchain(client, monkeypatch):
    _seed_job(client)
    from app import server as srv
    monkeypatch.setattr(resume_yamlcv, "available", lambda: False)
    r = client.post("/api/resume/build", json={"job_id": 1})
    assert r.status_code == 503 and "rendercv" in r.json()["detail"]


def test_build_422_when_loop_fails(client, monkeypatch):
    _seed_job(client)
    from app import server as srv
    monkeypatch.setattr(resume_yamlcv, "available", lambda: True)
    monkeypatch.setattr(resume_digest, "digest",
                        lambda s, **kw: {"sources": [{"file": "x", "chars": 100, "text": "t"}],
                                         "bullets": ["b"], "terms": ["sql"],
                                         "images": [], "chars": 500})
    monkeypatch.setattr(resume_digest, "corpus_text", lambda c, **kw: "C")
    monkeypatch.setattr(srv.LLMClient, "from_config",
                        classmethod(lambda cls, cfg: object()))

    def fail(llm, corpus, identity, job, out_dir, max_rounds=4):
        return {"ok": False, "pages": 2, "rounds": 4, "yaml": "y",
                "pdf": None, "history": [], "error": "still over one page after max rounds"}
    monkeypatch.setattr(resume_yamlcv, "build_one_pager", fail)
    r = client.post("/api/resume/build", json={"job_id": 1})
    assert r.status_code == 422 and "one page" in r.json()["detail"]


def test_built_list_empty_and_name_traversal_blocked(client):
    assert client.get("/api/resume/built").json() == {"items": []}
    assert client.get("/api/resume/built/..%2Fsecret").status_code in (400, 404)


def test_yamlcv_status_endpoint(client, monkeypatch):
    from app import server as srv
    monkeypatch.setattr(resume_yamlcv, "available", lambda: True)
    assert client.get("/api/resume/yamlcv-status").json() == {"available": True}
