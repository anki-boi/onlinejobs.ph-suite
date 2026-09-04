"""
resumes/schema.py — master resume: one canonical JSON document, the source of
truth the LLM tailors from. Deliberately flat and boring: LLMs emit simple
typed JSON reliably; the jsonresume.org spec adds ceremony nobody consumes.
"""

import json
from pathlib import Path

REQUIRED_BASIC = ("name", "email", "phone")


def validate(m) -> list:
    """Return a list of human-readable problems (empty = valid)."""
    errs = []
    if not isinstance(m, dict):
        return ["resume must be a JSON object"]
    b = m.get("basics")
    if not isinstance(b, dict):
        errs.append("basics: missing or not an object")
        b = {}
    for k in REQUIRED_BASIC:
        if not str(b.get(k) or "").strip():
            errs.append(f"basics.{k}: required")
    if not str(b.get("summary") or "").strip():
        errs.append("basics.summary: required")
    if not isinstance(m.get("skills"), list) or not all(isinstance(s, str) for s in m.get("skills", [])):
        errs.append("skills: must be a list of strings")
    for i, w in enumerate(m.get("work") or []):
        if not isinstance(w, dict) or not str(w.get("role") or "").strip():
            errs.append(f"work[{i}].role: required")
        if not isinstance(w.get("bullets"), list):
            errs.append(f"work[{i}].bullets: must be a list")
    for i, e in enumerate(m.get("education") or []):
        if not isinstance(e, dict) or not str(e.get("school") or "").strip():
            errs.append(f"education[{i}].school: required")
    return errs


def load_master(path) -> dict:
    p = Path(path)
    if not p.exists():
        return seed_master(p)
    return json.loads(p.read_text(encoding="utf-8"))


def save_master(path, m) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def seed_master(path) -> dict:
    """First-run placeholder — realistic shape, content to be replaced."""
    m = {
        "basics": {
            "name": "Juan Garcia",
            "email": "juan.garcia@example.com",
            "phone": "+63 900 000 0000",
            "location": "Cebu City, PH",
            "summary": "Virtual assistant with experience in admin support, bookkeeping, "
                       "and email handling for US-based clients.",
        },
        "skills": ["Bookkeeping", "Data Entry", "Email Support", "Microsoft Excel",
                   "Google Sheets", "Customer Service"],
        "work": [
            {
                "role": "Virtual Assistant",
                "company": "Previous Company",
                "start": "2023-01",
                "end": "Present",
                "bullets": [
                    "Handled 50+ emails/day with sub-one-hour response times",
                    "Maintained client ledgers and monthly P&L summaries in Excel",
                ],
            },
        ],
        "education": [
            {"school": "Your University", "degree": "BS Information Technology", "year": "2022"},
        ],
    }
    save_master(path, m)
    return m


# ── Multi-profile masters ───────────────────────────────────────────────
#
# masters.json: {"default": <name>, "profiles": {<name>: <master doc>}}.
# Lets one person keep several track resumes (healthcare / tech-data / …)
# and auto-pick the best fit per job with the deterministic ATS score.

def load_masters(path) -> dict:
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {"default": "master", "profiles": {"master": load_master(p.parent / "master.json")}}
        save_masters(p, data)
    return data


def save_masters(path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_profile(data: dict, name: str = "") -> dict:
    """Return one master doc. Empty name → default profile."""
    profiles = data.get("profiles") or {}
    name = (name or data.get("default") or next(iter(profiles), "")).strip()
    if name not in profiles:
        raise KeyError(name)
    return profiles[name]


def validate_masters(data: dict) -> list:
    errs = []
    for name, doc in (data.get("profiles") or {}).items():
        for e in validate(doc):
            errs.append(f"{name}: {e}")
    if data.get("default") not in (data.get("profiles") or {}):
        errs.append(f"default profile '{data.get('default')}' not in profiles")
    return errs


def best_profile_for_job(data: dict, job: dict):
    """Deterministic auto-pick: score every profile against the job, keep the max.
    Returns (profile_name, score_dict)."""
    from . import ats  # local import: ats stays dependency-free of schema
    best = None
    for name, doc in (data.get("profiles") or {}).items():
        s = ats.score_resume(ats.resume_to_text(doc), job)
        if best is None or s["total"] > best[1]["total"]:
            best = (name, s)
    return best
