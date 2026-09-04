"""
resumes/tailor.py — LLM tailoring: master resume + one job → tailored resume
JSON. The industry-wide rule, enforced in the prompt: rewrite and reorder
existing content to foreground the job's requirements; NEVER invent facts,
skills, employers, or numbers.
"""

import json
import urllib.request
import urllib.error

from .schema import validate

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


def tailor(resume: dict, job: dict, llm) -> dict:
    """Tailor `resume` for `job` via `llm` (duck-typed: .chat(messages) -> str).
    Any failure or invalid output → return the master unaltered."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"MASTER RESUME (JSON):\n{json.dumps(resume, ensure_ascii=False)}\n\n"
            f"JOB:\n{job_brief(job)}\n\nReturn the tailored resume JSON."},
    ]
    try:
        raw = llm.chat(messages)
    except Exception:
        return resume
    raw = str(raw).strip()
    if raw.startswith("```"):  # some models fence JSON anyway
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        out = json.loads(raw)
    except Exception:
        return resume
    if validate(out):
        return resume
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
