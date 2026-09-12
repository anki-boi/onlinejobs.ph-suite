"""tests/test_config_sources.py — resume_sources default + personal paths (W2.4).

The default resume source must be the repo's own resumes/ dir. A hard-coded
personal machine path (someone's cloud-drive folder) is meaningless on every
other machine and leaks the owner's layout in a public repo.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient

_SKIP_DIRS = {".git", ".venv", ".venv-rendercv", ".pytest_cache", "__pycache__",
              "backups", "node_modules"}

# Assembled so this file doesn't trip its own scan below.
_DROPBOX = "Dro" + "pbox"
_USERSPATH = "C:" + chr(92) + "Users" + chr(92)


def test_no_personal_paths_in_source_tree():
    bad = []
    for p in sorted(PROJECT_ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if _DROPBOX in text or _USERSPATH in text:
            bad.append(str(p.relative_to(PROJECT_ROOT)))
    assert bad == [], f"personal machine paths in tracked source: {bad}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", str(tmp_path / "test.db"))
    dbconn.init_db()
    from app import server as srv
    monkeypatch.setattr(srv, "_client", None)
    masters = tmp_path / "masters.json"
    masters.write_text('{"default": "master", "profiles": {"master": '
                       '{"name": "J D", "email": "a@b.com"}}}')
    monkeypatch.setattr(srv, "MASTERS_PATH", masters)
    monkeypatch.setattr(srv, "BUILT_DIR", tmp_path / "built")
    monkeypatch.setattr(srv, "BASE_RESUMES", tmp_path / "resumes")
    monkeypatch.setattr(srv, "_cfg", {})  # no resume_sources key
    with TestClient(srv.app) as c:
        yield c


def test_resume_sources_fallback_defaults_to_repo_resumes(client, monkeypatch, tmp_path):
    """With no resume_sources in config, the digest corpus comes from the
    repo's resumes/ dir only — no personal path in the list."""
    import app.server as srv
    import resumes.digest as digest_mod
    import resumes.yamlcv as yamlcv_mod

    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    def fake_digest(sources, **kw):
        fake_digest.sources = list(sources)
        return {"chars": 500, "text": "x" * 500}

    monkeypatch.setattr(digest_mod, "digest", fake_digest)
    monkeypatch.setattr(digest_mod, "corpus_text", lambda c, **kw: c["text"])
    monkeypatch.setattr(yamlcv_mod, "available", lambda: True)
    monkeypatch.setattr(yamlcv_mod, "build_one_pager",
                        lambda llm, corpus, ident, brief, out:
                        {"ok": True, "pdf": str(pdf), "yaml": "name: t", "history": [], "rounds": 1})
    monkeypatch.setattr(srv.LLMClient, "from_config", classmethod(lambda cls, cfg: object()))

    # Seed a job so the route gets past the 404 (mirrors test_yamlcv_api).
    from db.connection import get_conn
    from db.repos import jobs as job_repo
    conn = get_conn()
    job_repo.upsert_stub(conn, job_id=1, job_url="http://t/1", title="T")
    conn.commit()
    conn.close()

    res = client.post("/api/resume/build", json={"job_id": 1, "profile": "master"})
    assert res.status_code == 200, res.text
    assert fake_digest.sources == [str(tmp_path / "resumes")]
