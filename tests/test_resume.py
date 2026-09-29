"""
tests/test_resume.py — T5: schema, ATS scorer, tailor, renderer, API.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io
import pytest
from fastapi.testclient import TestClient
from app.server import app

from resumes.schema import load_master, save_master, validate, seed_master
from resumes.ats import score_resume, resume_to_text
from resumes.tailor import tailor
from resumes.render import to_docx, to_txt


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    import db.connection as dbconn
    monkeypatch.setattr(dbconn, "DB_PATH", db_path)
    dbconn.init_db()
    from app import server as srv
    monkeypatch.setattr(srv, "_client", None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def conn(client):
    import db.connection as dbconn
    return dbconn.get_conn()

MASTER = {
    "basics": {
        "name": "Juan Garcia",
        "email": "juan.garcia@example.com",
        "phone": "+63 917 000 0000",
        "location": "Cebu City, PH",
        "summary": "Virtual assistant with 3 years of experience in admin support, "
                   "bookkeeping, and data entry for US clients.",
    },
    "skills": ["Bookkeeping", "Data Entry", "Email Support", "Microsoft Excel",
               "Google Sheets", "Canva", "Customer Service", "Zoho"],
    "work": [
        {
            "role": "Virtual Assistant",
            "company": "Salhab Pharmacy",
            "start": "2023-01",
            "end": "Present",
            "bullets": [
                "Process 100+ daily patient orders into Zoho Books and update ledgers",
                "Answer and route 50+ emails/day with a <1h response time",
                "Prepare monthly P&L summaries in Excel for the owner",
            ],
        },
    ],
    "education": [
        {"school": "CTC University", "degree": "BS Information Technology", "year": "2022"},
    ],
}

JOB = {
    "title": "Bookkeeper",
    "employer": "Acme Co",
    "skills": ["Bookkeeping", "Zoho", "Data Entry", "QuickBooks", "Payroll"],
    "keywords": "bookkeeping, zoho books, payroll, data entry",
    "salary": "PHP 25,000 - PHP 40,000 / month",
    "description": "We need a full-time bookkeeper to maintain ledgers in Zoho Books "
                   "and run monthly payroll. Accurate data entry and Excel required.",
}


# ── schema ─────────────────────────────────────────────────────────────

def test_validate_good_master():
    assert validate(json.loads(json.dumps(MASTER))) == []


def test_validate_missing_email():
    m = json.loads(json.dumps(MASTER))
    m["basics"].pop("email")
    assert any("email" in e for e in validate(m))


def test_validate_bad_types():
    m = json.loads(json.dumps(MASTER))
    m["skills"] = "not-a-list"
    assert validate(m)


def test_seed_and_roundtrip(tmp_path):
    p = tmp_path / "master.json"
    seed_master(p)
    assert p.exists()
    m = load_master(p)
    assert validate(m) == []
    m["basics"]["name"] = "Changed"
    save_master(p, m)
    assert load_master(p)["basics"]["name"] == "Changed"


# ── ats scorer ────────────────────────────────────────────────────────

def test_resume_to_text_contains_all_parts():
    t = resume_to_text(MASTER)
    for needle in ("Juan Garcia", "juan.garcia@example.com", "Bookkeeping",
                   "Salhab Pharmacy", "Process 100+ daily patient orders",
                   "CTC University"):
        assert needle in t, needle


def test_score_perfectish_high():
    r = score_resume(resume_to_text(MASTER), JOB)
    assert r["total"] >= 60
    assert "Bookkeeping" in r["matched_skills"]
    assert "QuickBooks" in r["missing_skills"]


def test_score_unrelated_low():
    other = resume_to_text({
        "basics": {"name": "X", "email": "x@y.z", "phone": "123", "location": "L",
                   "summary": "Graphic designer."},
        "skills": ["Photoshop", "Illustrator"],
        "work": [], "education": [],
    })
    r = score_resume(other, JOB)
    assert r["total"] < 45


def test_score_breakdown_keys_and_suggestions():
    r = score_resume(resume_to_text(MASTER), JOB)
    assert set(r["breakdown"]) == {"skills", "keywords", "format", "completeness"}
    assert sum(r["breakdown"].values()) == r["total"]
    assert isinstance(r["suggestions"], list) and r["suggestions"]
    assert 0 <= r["total"] <= 100


def test_score_no_skills_field_in_job():
    job = {**JOB, "skills": None, "keywords": ""}
    r = score_resume(resume_to_text(MASTER), job)
    assert 0 <= r["total"] <= 100


# ── tailor (fake LLM) ─────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload or MASTER
        self.error = error
        self.last_messages = None

    def chat(self, messages):
        self.last_messages = messages
        if self.error:
            raise self.error
        return json.dumps(self.payload)


def test_tailor_returns_llm_json_when_valid():
    tailored = dict(json.loads(json.dumps(MASTER)))
    tailored["basics"]["summary"] = "Bookkeeper experienced with Zoho Books and payroll."
    out = tailor(MASTER, JOB, FakeLLM(payload=tailored))
    assert out["basics"]["summary"].startswith("Bookkeeper")
    assert validate(out) == []


def test_tailor_prompt_contains_job_context_and_no_fabrication_rule():
    llm = FakeLLM()
    tailor(MASTER, JOB, llm)
    text = " ".join(m["content"] for m in llm.last_messages)
    assert "Bookkeeper" in text and "QuickBooks" in text
    assert "invent" in text.lower() or "fabricate" in text.lower()


def test_tailor_bad_json_falls_back_to_master():
    out = tailor(MASTER, JOB, FakeLLM(payload={"not": "the schema"}))
    assert out == MASTER


def test_tailor_llm_error_falls_back_to_master():
    out = tailor(MASTER, JOB, FakeLLM(error=RuntimeError("500")))
    assert out == MASTER


# ── renderer ──────────────────────────────────────────────────────────

def test_to_txt_matches_resume_to_text():
    assert to_txt(MASTER) == resume_to_text(MASTER)


def test_to_docx_roundtrip():
    if pytest.importorskip("docx") is None:
        pytest.skip("python-docx not installed")
    data = to_docx(MASTER)
    assert data[:2] == b"PK"  # docx is a zip
    import docx
    d = docx.Document(io.BytesIO(data))
    full = "\n".join(p.text for p in d.paragraphs)
    assert "Juan Garcia" in full
    assert "Salhab Pharmacy" in full
    assert "Process 100+ daily patient orders" in full


# ── API ───────────────────────────────────────────────────────────────

def test_resume_endpoints(client, conn, tmp_path, monkeypatch):
    mp = tmp_path / "master.json"
    monkeypatch.setattr("app.server.RESUME_PATH", mp)
    monkeypatch.setattr("app.server.MASTERS_PATH", tmp_path / "masters.json")
    monkeypatch.setattr("app.server._cfg", {})  # no LLM in tests → tailor 503, no real network
    seed_master(mp)

    r = client.get("/api/resume")
    assert r.status_code == 200 and r.json()["basics"]["name"]

    r = client.get("/api/resume/ats", params={"job_id": 1})
    assert r.status_code == 404

    c = conn.execute(
        "INSERT INTO jobs (job_id, job_url, title, status, search_keyword, skills, salary, description, scrape_status) "
        "VALUES (9000, 'http://x', 'Bookkeeper', 'Open', ?, ?, ?, ?, 'Open')",
        (JOB["keywords"], json.dumps(JOB["skills"]), JOB["salary"], JOB["description"]),
    ).lastrowid
    conn.commit()

    r = client.get("/api/resume/ats", params={"job_id": c})
    assert r.status_code == 200
    assert 0 <= r.json()["total"] <= 100

    r = client.get("/api/resume/export", params={"job_id": c, "fmt": "txt"})
    assert r.status_code == 200
    assert "Juan Garcia" in r.text  # seeded placeholder

    r = client.get("/api/resume/export", params={"job_id": c, "fmt": "docx"})
    assert r.status_code == 200
    assert r.content[:2] == b"PK"

    # tailor without LLM configured → 503 with clear message
    r = client.post("/api/resume/tailor", json={"job_id": c})
    assert r.status_code == 503


def test_profiles_endpoint_and_auto_pick(client, conn, tmp_path, monkeypatch):
    """Multiple named profiles; auto=1 deterministically picks the best fit."""
    mp = tmp_path / "master.json"
    monkeypatch.setattr("app.server.RESUME_PATH", mp)
    monkeypatch.setattr("app.server.MASTERS_PATH", tmp_path / "masters.json")
    monkeypatch.setattr("app.server._cfg", {})
    seed_master(mp)  # becomes the default profile "master"

    # Add a healthcare-flavored profile.
    doc = json.loads(json.dumps(MASTER))
    doc["basics"]["summary"] = "Registered Pharmacist, hospital and community pharmacy intern, " \
                                "medical terminology, patient counseling, drug information, pharmacovigilance."
    doc["skills"] = ["Medical Terminology", "Patient Counseling", "Pharmacovigilance", "IPQC", "cGMP"]
    r = client.put("/api/resume", json={"master": doc, "profile": "healthcare"})
    assert r.status_code == 200

    r = client.get("/api/resume/profiles")
    assert r.status_code == 200
    assert set(r.json()["profiles"]) >= {"master", "healthcare"}

    r = client.get("/api/resume", params={"profile": "healthcare"})
    assert r.json()["skills"][0] == "Medical Terminology"

    r = client.get("/api/resume", params={"profile": "nope"})
    assert r.status_code == 404

    # A bookkeeping job → the pharmacy profile should NOT win auto-pick.
    c = conn.execute(
        "INSERT INTO jobs (job_id, job_url, title, status, search_keyword, skills, salary, description, scrape_status) "
        "VALUES (9001, 'http://x', 'Bookkeeper', 'Open', ?, ?, ?, ?, 'Open')",
        (JOB["keywords"], json.dumps(JOB["skills"]), JOB["salary"], JOB["description"]),
    ).lastrowid
    conn.commit()
    r = client.get("/api/resume/ats", params={"job_id": c, "auto": 1})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"profile", "total"}
    assert body["profile"] != "healthcare"

    # tailor auto=1 with a mocked LLM reports the auto-picked profile.
    class _L:
        def chat(self, messages):
            return json.dumps(MASTER)
    monkeypatch.setattr("app.server.LLMClient.from_config", staticmethod(lambda cfg: _L()))
    r = client.post("/api/resume/tailor", json={"job_id": c, "auto": 1})
    assert r.status_code == 200
    assert r.json()["profile"] and r.json()["score"]["total"] > 0


def test_jobs_min_ats_filter(client, conn, tmp_path, monkeypatch):
    """?min_ats=50 hides jobs every master profile scores below 50 on."""
    import app.server as srv
    from db.repos import jobs as job_repo
    monkeypatch.setattr(srv, "MASTERS_PATH", Path(__file__).parent.parent / "resumes" / "masters.json")
    # Job A: skills two of his profiles have (Excel + Data Entry-ish) — expect >= 50
    job_repo.upsert_stub(conn, job_id=1, job_url="http://a", title="Excel & Data Entry Assistant",
                         skills=["Excel", "Data Entry"])
    # Job B: skills none of his profiles have — expect < 50
    job_repo.upsert_stub(conn, job_id=2, job_url="http://b", title="Welder & Carpenter",
                         skills=["Welding", "Carpentry"])
    conn.commit()
    r = client.get("/api/jobs", params={"min_ats": 50, "per_page": 500})
    assert r.status_code == 200
    body = r.json()
    kept = {j["title"] for j in body["items"]}
    assert "Excel & Data Entry Assistant" in kept
    assert "Welder & Carpenter" not in kept
    assert body["total"] == 1
    # sanity: the two scores actually straddle the threshold
    sA = client.get("/api/resume/ats", params={"job_id": 1, "auto": 1}).json()["total"]
    sB = client.get("/api/resume/ats", params={"job_id": 2, "auto": 1}).json()["total"]
    assert sA >= 50 > sB


def test_resume_form_roundtrip_template(client, tmp_path, monkeypatch):
    """Shareability: a fresh clone (no masters.json) starts from the tracked
    starter template, and the in-UI form (PUT basics+skills) persists + reads back."""
    import shutil
    repo_template = Path(__file__).parent.parent / "resumes" / "master.json"
    mp = tmp_path / "master.json"
    shutil.copy(repo_template, mp)
    monkeypatch.setattr("app.server.RESUME_PATH", mp)
    monkeypatch.setattr("app.server.MASTERS_PATH", tmp_path / "masters.json")  # absent -> fallback to master.json
    monkeypatch.setattr("app.server._cfg", {})

    r = client.get("/api/resume")
    assert r.status_code == 200
    assert r.json()["basics"]["name"] == "Juan Garcia"  # the starter placeholder

    # What the form submits: edited basics + skills, work/education carried through unchanged.
    base = r.json()
    body = {
        "profile": "",  # empty -> saved to the default profile
        "master": {
            "basics": {"name": "A Stranger", "email": "stranger@example.com",
                       "phone": "+1 555 0000", "location": "Somewhere",
                       "summary": "I automate repetitive admin work."},
            "skills": ["Data entry", "Email management", "n8n"],
            "work": base.get("work", []),
            "education": base.get("education", []),
        },
    }
    assert client.put("/api/resume", json=body).status_code == 200

    r = client.get("/api/resume")
    assert r.json()["basics"]["name"] == "A Stranger"
    assert "n8n" in r.json()["skills"]
    assert r.json()["work"][0]["role"] == "Your Job Title"  # template work preserved


# ── B10: the anti-hallucination guard (audit) ───────────────────────────────

from resumes.tailor import faithfulness


class CountingLLM:
    """Returns `payload` on every call and counts them, so a test can prove the
    document was tailored once, not re-rolled at download time."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return json.dumps(self.payload)


def _doctored(**changes):
    out = json.loads(json.dumps(MASTER))
    out.update(changes)
    return out


def test_guard_rejects_an_invented_employer():
    out = _doctored(work=MASTER["work"] + [
        {"role": "Data Analyst", "company": "Acme Corp", "start": "2024",
         "end": "Present", "bullets": ["Built dashboards"]}])
    assert "Acme Corp" in faithfulness(MASTER, out)


def test_guard_rejects_an_invented_date():
    out = _doctored(work=[dict(MASTER["work"][0], start="2019")])
    assert "date" in faithfulness(MASTER, out)


def test_guard_rejects_an_invented_number():
    """The classic hallucination: a metric the resume never claimed."""
    out = _doctored(work=[dict(MASTER["work"][0], bullets=[
        "Raised retention by 38% across 12 accounts"])])
    assert "numbers" in faithfulness(MASTER, out)


def test_guard_rejects_an_invented_skill():
    out = _doctored(skills=MASTER["skills"] + ["Figma"])
    assert "Figma" in faithfulness(MASTER, out)


def test_guard_rejects_an_invented_school():
    out = _doctored(education=MASTER.get("education", []) + [
        {"school": "Harvard", "degree": "MBA", "year": "2026"}])
    assert "school" in faithfulness(MASTER, out)


def test_guard_accepts_rewording_that_keeps_the_facts():
    """Reordering and rephrasing are the whole point of tailoring — the guard must
    not reject them, or the feature is dead."""
    out = json.loads(json.dumps(MASTER))
    out["basics"]["summary"] = "Bookkeeper experienced with Zoho Books and payroll."
    out["skills"] = list(reversed(MASTER["skills"]))
    out["work"] = [dict(MASTER["work"][0], bullets=list(reversed(MASTER["work"][0]["bullets"])))]
    assert faithfulness(MASTER, out) is None


def test_tailor_falls_back_to_the_master_and_says_why():
    bad = _doctored(work=MASTER["work"] + [
        {"role": "VA", "company": "Made Up Co", "start": "2020", "end": "2021",
         "bullets": ["Did things"]}])
    report = {}
    out = tailor(MASTER, JOB, FakeLLM(payload=bad), report)
    assert out == MASTER, "an invented employer must not reach the document"
    assert "Made Up Co" in report["guard"]


def test_tailor_keeps_a_faithful_rewrite():
    good = _doctored(basics=dict(MASTER["basics"],
                                 summary="Bookkeeper experienced with Zoho Books."))
    report = {}
    out = tailor(MASTER, JOB, FakeLLM(payload=good), report)
    assert out["basics"]["summary"].startswith("Bookkeeper experienced")
    assert report["guard"] is None


# ── B9: the download is the document that was scored ────────────────────────

def test_two_downloads_are_byte_identical(client, conn, tmp_path, monkeypatch):
    """Export used to call tailor() again — a second LLM roll at temperature 0.2 —
    so the .docx was not the resume whose ATS score you had just been shown."""
    mp = tmp_path / "master.json"
    monkeypatch.setattr("app.server.RESUME_PATH", mp)
    monkeypatch.setattr("app.server.MASTERS_PATH", tmp_path / "masters.json")
    seed_master(mp)
    # The stored master is what the guard compares against, so register the
    # fixture as the "master" profile instead of the seeded placeholder.
    assert client.put("/api/resume", json={"master": MASTER, "profile": "master"}).status_code == 200
    doc = json.loads(json.dumps(MASTER))
    doc["basics"]["summary"] = "Bookkeeper experienced with Zoho Books and payroll."
    llm = CountingLLM(doc)
    monkeypatch.setattr("app.server.LLMClient.from_config", staticmethod(lambda cfg: llm))
    monkeypatch.setattr("app.server._cfg", {"llm_base_url": "http://x"})

    pk = conn.execute(
        "INSERT INTO jobs (job_id, job_url, title, status, search_keyword, skills, salary, description, scrape_status) "
        "VALUES (9100, 'http://x', 'Bookkeeper', 'Open', ?, ?, ?, ?, 'Open')",
        (JOB["keywords"], json.dumps(JOB["skills"]), JOB["salary"], JOB["description"]),
    ).lastrowid
    conn.commit()

    r = client.post("/api/resume/tailor", json={"job_id": pk, "profile": "master"})
    assert r.status_code == 200 and r.json()["stored"] is True
    first = client.get("/api/resume/export",
                       params={"job_id": pk, "fmt": "txt", "tailored": 1, "profile": "master"})
    second = client.get("/api/resume/export",
                        params={"job_id": pk, "fmt": "txt", "tailored": 1, "profile": "master"})
    assert first.status_code == second.status_code == 200
    assert first.text == second.text, "two downloads of the same tailored resume differ"
    assert "Zoho Books and payroll" in first.text
    assert llm.calls == 1, "the LLM ran once; export served the stored document"


def test_export_needs_no_llm_once_the_tailoring_is_stored(client, conn, tmp_path, monkeypatch):
    mp = tmp_path / "master.json"
    monkeypatch.setattr("app.server.RESUME_PATH", mp)
    monkeypatch.setattr("app.server.MASTERS_PATH", tmp_path / "masters.json")
    seed_master(mp)
    assert client.put("/api/resume", json={"master": MASTER, "profile": "master"}).status_code == 200
    doc = json.loads(json.dumps(MASTER))
    doc["basics"]["summary"] = "Bookkeeper with Zoho Books."
    llm = CountingLLM(doc)
    monkeypatch.setattr("app.server.LLMClient.from_config", staticmethod(lambda cfg: llm))
    monkeypatch.setattr("app.server._cfg", {"llm_base_url": "http://x"})

    pk = conn.execute(
        "INSERT INTO jobs (job_id, job_url, title, status, search_keyword, skills, salary, description, scrape_status) "
        "VALUES (9101, 'http://x', 'Bookkeeper', 'Open', ?, ?, ?, ?, 'Open')",
        (JOB["keywords"], json.dumps(JOB["skills"]), JOB["salary"], JOB["description"]),
    ).lastrowid
    conn.commit()
    client.post("/api/resume/tailor", json={"job_id": pk, "profile": "master"})

    def explode(cfg):
        raise AssertionError("export must not re-run the LLM")
    monkeypatch.setattr("app.server.LLMClient.from_config", staticmethod(explode))

    r = client.get("/api/resume/export",
                   params={"job_id": pk, "fmt": "txt", "tailored": 1, "profile": "master"})
    assert r.status_code == 200 and "Zoho Books" in r.text


def test_v10_table_exists_after_migrate(client):
    import db.connection as dbconn
    conn = dbconn.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tailored_resumes" in tables
