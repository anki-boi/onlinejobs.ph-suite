"""Build a 1-page Harvard-theme CV as rendercv YAML, via LLM + re-iterate loop.

The LLM is only allowed to use facts from the digest corpus. The render loop
checks the real PDF page count and feeds it back until the result is exactly
one page (or max_rounds is hit).
"""
import os
import re
import subprocess
from pathlib import Path

import pymupdf

BASE_DIR = Path(__file__).resolve().parent.parent

if os.name == "nt":
    RENDERCV_BIN = BASE_DIR / ".venv-rendercv" / "Scripts" / "rendercv.exe"
else:
    RENDERCV_BIN = BASE_DIR / ".venv-rendercv" / "bin" / "rendercv"


def available() -> bool:
    return RENDERCV_BIN.exists()


STYLE = """You are a resume writer producing rendercv YAML (theme: harvard).
Rules — these are absolute:
1. EXACTLY ONE US-LETTER page. This is enforced by rendering; if it overflows
   you will be asked to cut. Prefer fewer, sharper items over coverage.
2. Use ONLY facts present in the FACT CORPUS. Never invent employers, dates,
   numbers, tools, or achievements. You may reword and reorder.
3. Target the JOB given. Lead with the most relevant experience and skills;
   cut whatever is least relevant to that job.
4. YAML shape (rendercv v2, extra keys are rejected):
   cv:
     name: <string>
     headline: <one short line, e.g. "Clinical Data & Automation Specialist">
     location: <city, country>
     email: <string>
     phone: <string>
     website: <url or empty>
     social_networks:            # 0-3 items only
       - network: LinkedIn
         username: <handle>
     sections:
       profile:                  # 2-3 sentences max
         - <sentence>
       experience:               # max 3 entries, most recent first
         - company: <string>
           position: <string>
           start_date: <YYYY-MM>
           end_date: <YYYY-MM or present>
           location: <city>
           highlights:           # max 3 items, one line each, <= 90 chars,
             - <one-line achievement, verb-first, with a number when the corpus has one>
       education:                # max 2 entries
         - institution: <string>
           area: <field of study>
           degree: <string>
           start_date: <YYYY-MM>
           end_date: <YYYY-MM>
           location: <city>
       skills:                   # 8-14 short skill phrases, comma-separated strings
         - <skill>
       certifications:           # only if in the corpus; max 4 one-liners
         - <string>
   design:
     theme: harvard
   Do NOT include date fields with both date and start_date; use start_date/end_date only.
5. No photos, no custom_connections, no social networks you are not sure of.
6. Reply with ONLY a ```yaml fenced block. No commentary outside the fence."""


def render(yaml_text: str, out_dir: Path) -> dict:
    """Write YAML, run rendercv, return {ok, pages, pdf} or {ok: False, error}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    y = out_dir / "cv.yaml"
    y.write_text(yaml_text, encoding="utf-8")
    pdf_path = out_dir / "rendercv_output" / "cv.pdf"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(
            [str(RENDERCV_BIN), "render", "cv.yaml",
             "--pdf-path", str(pdf_path), "--typst-path", str(out_dir / "cv.typ"),
             "--dont-generate-markdown", "--dont-generate-png"],
            capture_output=True, text=True, cwd=str(out_dir), env=env, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "rendercv timed out"}
    pdf = pdf_path
    if not pdf.exists():
        err = (p.stderr or p.stdout or "")
        m = re.search(r"(?:Error|error)[^\n]*(?:\n[^\n]+){0,6}", err)
        return {"ok": False, "error": (m.group(0) if m else err)[-800:]}
    d = pymupdf.open(str(pdf))
    pages = len(d)
    d.close()
    return {"ok": True, "pages": pages, "pdf": str(pdf)}


def _fenced_yaml(out: str) -> str:
    m = re.search(r"```ya?ml\r?\n(.*?)```", out, re.S)
    return (m.group(1) if m else out).strip()


def build_one_pager(llm, corpus_text: str, identity: str, job_text: str,
                    out_dir: Path, max_rounds: int = 4) -> dict:
    """LLM draft -> render -> page check -> feed back until 1 page.

    Returns {ok, pages, rounds, yaml, pdf, history}.
    """
    system = STYLE
    user = (
        f"IDENTITY (from the user's master resume — use verbatim for name/contact):\n"
        f"{identity}\n\n"
        f"JOB TO TARGET:\n{job_text}\n\n"
        f"FACT CORPUS (the ONLY facts you may use):\n{corpus_text}\n\n"
        "Write the YAML."
    )
    yaml_text = _fenced_yaml(llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]))
    history, last_pdf = [], None
    for rnd in range(1, max_rounds + 1):
        r = render(yaml_text, out_dir / f"round{rnd}")
        if r.get("pdf"):
            last_pdf = r["pdf"]
        history.append({"round": rnd, **r})
        if r["ok"] and r["pages"] <= 1:
            return {"ok": True, "pages": 1, "rounds": rnd,
                    "yaml": yaml_text, "pdf": r["pdf"], "history": history}
        problem = f"{r['pages']} pages" if r["ok"] else f"render failed: {r['error']}"
        user = (
            f"Your previous YAML produced {problem}. It must be exactly 1 page.\n"
            f"Fix it: if it overflows, DROP the least job-relevant section items "
            f"and shorten highlights to single lines; if it failed to render, fix "
            f"the YAML structure (see the schema in the system rules). Keep the "
            f"same facts — do not add anything new.\n\nPrevious YAML:\n{yaml_text}"
        )
        yaml_text = _fenced_yaml(llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]))
    return {"ok": False, "pages": history[-1].get("pages", 0),
            "rounds": max_rounds, "yaml": yaml_text, "pdf": last_pdf,
            "history": history, "error": "still over one page after max rounds"}
