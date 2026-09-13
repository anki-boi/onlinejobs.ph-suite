"""tests/test_install.py — Fresh-install correctness (W1.1).

A clean machine runs `pip install -r requirements.txt` and boots.
`resumes.digest` / `resumes.yamlcv` import pymupdf at module top, so
app.server must not import them eagerly: it must import, and the resume
routes must degrade, even where pymupdf is missing.
"""

import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Modules that import pymupdf at their top; must be re-importable fresh below.
_PYMUPDF_MODS = [("resumes.digest", "digest"), ("resumes.yamlcv", "yamlcv")]


@pytest.fixture
def no_pymupdf(monkeypatch):
    """Make `import pymupdf` fail, as on a machine where it is not installed.

    Submodules are unbound from both sys.modules and their parent package —
    `from resumes import yamlcv` resolves via the package attribute, which
    would otherwise skip re-execution and the pymupdf import entirely.
    """
    import resumes
    for mod, attr in _PYMUPDF_MODS:
        monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.delattr(resumes, attr, raising=False)
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    yield


def test_import_app_server_without_pymupdf(no_pymupdf):
    import app as app_pkg
    saved = sys.modules.pop("app.server", None)
    saved_attr = getattr(app_pkg, "server", None)
    try:
        fresh = importlib.import_module("app.server")
    finally:
        # importlib also rebinds the parent package's `server` attribute —
        # restore it or later tests import a different module object.
        if saved is not None:
            sys.modules["app.server"] = saved
            app_pkg.server = saved_attr
    assert fresh is not saved, "expected a fresh module object"
    from fastapi import FastAPI
    assert isinstance(fresh.app, FastAPI)


def test_yamlcv_status_without_pymupdf(no_pymupdf):
    from fastapi.testclient import TestClient
    from app import server as srv

    with TestClient(srv.app) as c:
        res = c.get("/api/resume/yamlcv-status")
    assert res.status_code == 200
    assert res.json() == {"available": False}


def test_resume_build_without_pymupdf(no_pymupdf):
    from fastapi.testclient import TestClient
    from app import server as srv

    with TestClient(srv.app) as c:
        res = c.post("/api/resume/build", json={"job_id": 1, "profile": "p"})
    assert res.status_code == 503
    assert "pymupdf" in res.json()["error"]["message"]
