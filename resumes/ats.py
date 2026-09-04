"""
resumes/ats.py — deterministic ATS-style scoring. Deliberately NOT an LLM:
recruiter-side ATS first pass is keyword/structure based, and a rule-based
score can't hallucinate. Scores a resume text against a specific job.

Scale (100): skills 40 · keywords 20 · format 25 · completeness 15.
"""

import json
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(\+?63|0)[\s-]?9\d{2}[\s-]?\d{3}[\s-]?\d{4}|[\d][\d\s-]{8,14}\d")
SECTION_WORDS = ("summary", "experience", "skills", "education")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _norm_tok(s) -> str:
    return re.sub(r"[^a-z0-9+#]+", "", str(s or "").lower())


def resume_to_text(m: dict) -> str:
    """Render the master (or tailored) JSON as one-column plain text with
    standard section headings — the layout ATS parsers handle best."""
    b = m.get("basics") or {}
    lines = []
    lines.append(str(b.get("name") or "").strip())
    contact = " | ".join(x for x in (b.get("email"), b.get("phone"), b.get("location")) if x)
    if contact:
        lines.append(contact)
    if b.get("summary"):
        lines += ["", "SUMMARY", str(b["summary"]).strip()]
    if m.get("skills"):
        lines += ["", "SKILLS", ", ".join(m["skills"])]
    if m.get("work"):
        lines.append("")
        lines.append("EXPERIENCE")
        for w in m["work"]:
            when = " - ".join(x for x in (w.get("start"), w.get("end")) if x)
            lines.append(f"{w.get('role', '')}{(' at ' + w['company']) if w.get('company') else ''}"
                         f"{(' (' + when + ')') if when else ''}")
            for bl in w.get("bullets") or []:
                lines.append(f"  - {bl}")
    if m.get("education"):
        lines += ["", "EDUCATION"]
        for e in m["education"]:
            extra = " - ".join(x for x in (e.get("degree"), e.get("year")) if x)
            lines.append(f"{e.get('school', '')}{(' - ' + extra) if extra else ''}")
    return "\n".join(lines).strip() + "\n"


def _skill_in(skill: str, text_low: str) -> bool:
    t = _norm_tok(skill)
    if not t:
        return False
    return t in _norm_tok(text_low)


def score_resume(text: str, job: dict) -> dict:
    text_low = _norm(text).lower()
    text_tok = _norm_tok(text_low)
    out = {"breakdown": {}, "matched_skills": [], "missing_skills": [], "suggestions": []}

    # ── skills (40) ──
    skills = job.get("skills")
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except Exception:
            skills = [s.strip() for s in skills.split(",")]
    skills = [s for s in (skills or []) if isinstance(s, str) and s.strip()]
    if skills:
        matched, missing = [], []
        for s in skills:
            (matched if _skill_in(s, text_low) else missing).append(s.strip())
        pts = round(40 * len(matched) / len(skills))
    else:
        matched, missing, pts = [], [], 0  # no data — no points, no penalty narrative
    out["matched_skills"], out["missing_skills"] = matched, missing
    out["breakdown"]["skills"] = pts

    # ── keywords (20) ──
    kws = [k.strip() for k in str(job.get("keywords") or "").split(",") if len(k.strip()) >= 3]
    if kws:
        hit = sum(1 for k in kws if _norm_tok(k) and _norm_tok(k) in text_tok)
        out["breakdown"]["keywords"] = round(20 * hit / len(kws))
    else:
        out["breakdown"]["keywords"] = 0

    # ── format (25): what the parser can actually chew ──
    f = 0
    if EMAIL_RE.search(text):
        f += 10
    if PHONE_RE.search(text):
        f += 5
    sections = [w for w in SECTION_WORDS if re.search(rf"(?m)^[# ]*{w}[:#]?\s*$", text, re.I)]
    f += int(10 * min(1, len(sections) / 3))  # 3 of {summary,experience,skills}
    words = len(text.split())
    if 300 <= words <= 1500:
        f += 5
    out["breakdown"]["format"] = f

    # ── completeness (15) ──
    c = 0
    if re.search(r"(?im)^summary[:#]?\s*$", text):
        c += 5
    if re.search(r"[\d]{4}(-|/| )?(0[1-9]|1[0-2])?", text):  # some date present
        c += 5
    if re.search(r"(?im)^(experience|work history|employment)[:#]?\s*$", text):
        c += 5
    out["breakdown"]["completeness"] = c

    out["total"] = sum(out["breakdown"].values())

    # ── rule-generated suggestions ──
    if missing:
        out["suggestions"].append(
            f"Add or evidence these job requirements: {', '.join(missing[:6])}")
    if "summary" not in sections:
        out["suggestions"].append("Add a SUMMARY section (recruiters + parsers both read it first)")
    if not EMAIL_RE.search(text):
        out["suggestions"].append("Include an email address in the header")
    if not (300 <= words <= 1500):
        out["suggestions"].append(
            f"Length is {words} words; ATS + recruiter sweet spot is ~400-800")
    return out
