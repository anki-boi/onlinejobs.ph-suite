"""
resumes/tailor.py — LLM tailoring: master resume + one job → tailored resume
JSON. The industry-wide rule, enforced in the prompt: rewrite and reorder
existing content to foreground the job's requirements; NEVER invent facts,
skills, employers, or numbers.

B10 (audit): the prompt is a wish, not a guard. `validate()` only checks SHAPE,
so a model that hallucinates "Acme Corp, 2021-2024, +38% conversion" passes and
the invented facts go straight into the exported document. `faithfulness()` is
the deterministic check that actually stops it — and the fallback path is the
same one already used for unparseable JSON: return the master.
"""

import json
import re
import urllib.request
import urllib.error

from .schema import validate

_NUM = re.compile(r"\d[\d,./-]*\d|\d")


def _norm_name(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _digit_runs(text: str) -> set:
    return set(_NUM.findall(str(text or "")))


def _master_facts(master: dict) -> dict:
    """Everything the master asserts: names, dates, numbers, skills."""
    work = master.get("work") or []
    edu = master.get("education") or []
    blob = json.dumps(master, ensure_ascii=False)
    return {
        "companies": {_norm_name(w.get("company")) for w in work if w.get("company")},
        "roles": {_norm_name(w.get("role")) for w in work if w.get("role")},
        "schools": {_norm_name(e.get("school")) for e in edu if e.get("school")},
        "dates": {str(w.get("start")) for w in work if w.get("start")} |
                 {str(w.get("end")) for w in work if w.get("end")} |
                 {str(e.get("year")) for e in edu if e.get("year")},
        "numbers": _digit_runs(blob),
        "skills": {_norm_name(s) for s in (master.get("skills") or [])},
    }


def faithfulness(master: dict, out: dict) -> str | None:
    """Return why `out` is not faithful to `master`, or None if it is.

    The rule the prompt promises, made checkable: no employer, role, school,
    date, skill or number that the master doesn't already contain. Reordering
    and rewording are free — facts are not."""
    f = _master_facts(master)

    for w in out.get("work") or []:
        c = _norm_name(w.get("company"))
        if c and c not in f["companies"]:
            return f"invented employer {w.get('company')!r}"
        r = _norm_name(w.get("role"))
        if r and r not in f["roles"]:
            return f"invented role {w.get('role')!r}"
        for k in ("start", "end"):
            v = w.get(k)
            if v and str(v) not in f["dates"]:
                return f"invented date {k}={v!r}"
    for e in out.get("education") or []:
        s = _norm_name(e.get("school"))
        if s and s not in f["schools"]:
            return f"invented school {e.get('school')!r}"
        if e.get("year") and str(e["year"]) not in f["dates"]:
            return f"invented year {e['year']!r}"

    for s in out.get("skills") or []:
        if _norm_name(s) not in f["skills"]:
            return f"invented skill {s!r}"

    new_numbers = _digit_runs(json.dumps(out, ensure_ascii=False)) - f["numbers"]
    if new_numbers:
        return f"invented numbers {sorted(new_numbers)[:4]}"
    return None

SYSTEM = """You are a resume tailoring engine. You receive a master resume (JSON)
and a job posting. Rewrite the master resume for THIS job:
- Rewrite the summary to lead with the job's title and its top requirements.
- Rewrite/reorder work bullets to foreground evidence matching the job's
  skills and keywords, keeping every fact exactly as given (numbers, dates,
  employers are sacred).
- You may reorder the skills list to put job-relevant skills first.
- NEVER invent skills, employers, metrics, or experience that are not in the
  master resume. If the job requires something the resume lacks, do not add it.
- Keep the exact JSON structure: basics{name,email,phone,location,summary},
  skills[str], work[{role,company,start,end,bullets[]}], education[{school,degree,year}].
Voice (the owner's own writing rules):
- Short and concise. Recruiters skim — no yapping, no filler.
- No corporate buzzwords: never "leverage", "synergy", "passionate",
  "results-driven", "proven track record".
- Confident but not boastful; humble but not self-deprecating.
- Concrete specifics beat adjectives: name the tool, the number, the outcome.
Return ONLY the JSON object — no prose, no markdown fences."""


def job_brief(job: dict) -> str:
    skills = job.get("skills")
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except Exception:
            skills = [s.strip() for s in skills.split(",")]
    parts = [
        f"Title: {job.get('title')}",
        f"Employer: {job.get('employer') or 'n/a'}",
        f"Skills required: {', '.join(skills or []) or 'n/a'}",
        f"Keywords: {job.get('keywords') or ''}",
        f"Salary: {job.get('salary') or 'n/a'}",
        f"Description: {str(job.get('description') or '')[:1500]}",
    ]
    return "\n".join(parts)


def tailor(resume: dict, job: dict, llm, report: dict | None = None) -> dict:
    """Tailor `resume` for `job` via `llm` (duck-typed: .chat(messages) -> str).
    Any failure, invalid output, or invented fact → return the master unaltered.
    `report`, if given, receives why (B10) so the UI can say "the LLM made up a
    company, so your master was kept" instead of silently shipping the guess."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"MASTER RESUME (JSON):\n{json.dumps(resume, ensure_ascii=False)}\n\n"
            f"JOB:\n{job_brief(job)}\n\nReturn the tailored resume JSON."},
    ]
    try:
        raw = llm.chat(messages)
    except Exception as exc:
        if report is not None:
            report["guard"] = f"LLM call failed: {type(exc).__name__}"
        return resume
    raw = str(raw).strip()
    if raw.startswith("```"):  # some models fence JSON anyway
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        out = json.loads(raw)
    except Exception:
        if report is not None:
            report["guard"] = "model did not return JSON"
        return resume
    if validate(out):
        if report is not None:
            report["guard"] = "model output failed schema validation"
        return resume
    reason = faithfulness(resume, out)
    if reason:
        if report is not None:
            report["guard"] = reason
        return resume
    if report is not None:
        report["guard"] = None
    return out


class LLMClient:
    """OpenAI-compatible chat client. Works with any /v1 endpoint: local
    llama-server/vLLM in WSL, OpenAI, DeepSeek, etc. Config keys:
    llm_base_url, llm_api_key, llm_model."""

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4o-mini", timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, messages) -> str:
        body = json.dumps({"model": self.model, "messages": messages,
                           "temperature": 0.2}).encode()
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"]["content"]

    @classmethod
    def from_config(cls, cfg: dict):
        if not cfg.get("llm_base_url"):
            return None
        return cls(cfg["llm_base_url"], cfg.get("llm_api_key", ""), cfg.get("llm_model", "gpt-4o-mini"))
