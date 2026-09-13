"""app/routers/resume.py — resume profiles, ATS scoring, LLM tailoring,
export, rendercv build. (W5.1 split; no behaviour change. All shared state is
read through the app.server module at call time so test monkeypatches keep
working.)"""

import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.routing import APIRouter
from app.schemas import ResumeUpdate, TailorRequest
from app import server as srv
from db.repos import jobs as job_repo

router = APIRouter(tags=['Resume'])


@router.get("/api/resume")
def get_resume(profile: str = ""):

    """Current resume text plus profile metadata."""

    try:
        return srv.resume_schema.get_profile(srv._masters(), profile)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")


@router.get("/api/resume/profiles")
def get_resume_profiles():

    """List saved resume profiles."""
    d = srv._masters()
    return {"default": d.get("default"), "profiles": list((d.get("profiles") or {}).keys())}


@router.put("/api/resume")
def put_resume(body: ResumeUpdate):

    """Save resume text (create or update a profile)."""
    errs = srv.validate(body.master)
    if errs:
        raise HTTPException(400, "; ".join(errs))
    d = srv._masters()
    name = (body.profile or d.get("default") or "master").strip()
    d.setdefault("profiles", {})[name] = body.master
    srv.resume_schema.save_masters(srv.MASTERS_PATH, d)
    return body.master


@router.get("/api/resume/ats")
def resume_ats(job_id: int, profile: str = "", auto: int = 0):

    """ATS pre-screen score for the resume against one job."""
    conn = srv.get_db()
    row = job_repo.get_job(conn, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = srv._job_dict_for_resume(row)
    d = srv._masters()
    if auto:
        name, score = srv.resume_schema.best_profile_for_job(d, jd)
        return {"profile": name, **score}
    try:
        doc = srv.resume_schema.get_profile(d, profile)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    return srv.resume_ats_mod.score_resume(srv.resume_render.to_txt(doc), jd)


@router.post("/api/resume/tailor")
def resume_tailor(body: TailorRequest):

    """LLM-tailor the resume for one job (SSE stream of progress)."""
    llm = srv.LLMClient.from_config(srv._cfg)
    if llm is None:
        raise HTTPException(503, "No LLM configured - add llm_base_url/llm_model to config.local.json")
    conn = srv.get_db()
    row = job_repo.get_job(conn, body.job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = srv._job_dict_for_resume(row)
    d = srv._masters()
    name = srv.resume_schema.best_profile_for_job(d, jd)[0] if body.auto \
        else (body.profile or d.get("default") or "").strip()
    try:
        doc = srv.resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    tailored = srv.tailor(doc, jd, llm)
    return {
        "profile": name,
        "tailored": tailored,
        "changed": tailored != doc,
        "score": srv.resume_ats_mod.score_resume(srv.resume_render.to_txt(tailored), jd),
    }


@router.get("/api/resume/export")
def resume_export(job_id: int, fmt: str = "docx", tailored: int = 0, profile: str = "", auto: int = 0):

    """Render the resume as a document for download."""
    conn = srv.get_db()
    row = job_repo.get_job(conn, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = srv._job_dict_for_resume(row)
    d = srv._masters()
    if auto:
        name = srv.resume_schema.best_profile_for_job(d, jd)[0]
    else:
        name = profile or d.get("default")
    try:
        m = srv.resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    if tailored:
        llm = srv.LLMClient.from_config(srv._cfg)
        if llm is None:
            raise HTTPException(503, "No LLM configured - add llm_base_url/llm_model to config.local.json")
        m = srv.tailor(m, jd, llm)
    if fmt == "txt":
        body, ctype, ext = srv.resume_render.to_txt(m), "text/plain; charset=utf-8", "txt"
    else:
        body, ctype, ext = srv.resume_render.to_docx(m), \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    safe = re.sub(r"[^A-Za-z0-9-]+", "_", row["title"] or "resume")[:40]
    return Response(content=body, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'})


@router.get("/api/resume/yamlcv-status")
def yamlcv_status():

    """Status of the yamlcv tooling."""
    try:
        from resumes import yamlcv as resume_yamlcv
    except ImportError:
        return {"available": False}
    return {"available": resume_yamlcv.available()}


@router.get("/api/resume/built")
def built_list():

    """List built resume files."""
    if not srv.BUILT_DIR.is_dir():
        return {"items": []}
    items = []
    for f in sorted(srv.BUILT_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        y = f.with_suffix(".yaml")
        items.append({
            "name": f.name,
            "mtime": f.stat().st_mtime,
            "has_yaml": y.exists(),
            "url": f"/api/resume/built/{f.name}",
        })
    return {"items": items}


@router.get("/api/resume/built/{name}")
def built_file(name: str):

    """Download one built resume file."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    f = srv.BUILT_DIR / name
    if not f.is_file():
        raise HTTPException(404, "not found")
    from fastapi.responses import FileResponse
    return FileResponse(f, media_type="application/pdf")


@router.post("/api/resume/build")
def resume_build(body: TailorRequest):

    """Build the current resume into a file."""
    try:
        from resumes import digest as resume_digest
        from resumes import yamlcv as resume_yamlcv
    except ImportError as e:
        raise HTTPException(503, f"{e.name} is not installed - pip install -r requirements.txt")
    if not resume_yamlcv.available():
        raise HTTPException(503, "rendercv toolchain missing - run install.bat (needs Python 3.12+)")
    llm = srv.LLMClient.from_config(srv._cfg)
    if llm is None:
        raise HTTPException(503, "No LLM configured - add llm_base_url/llm_model to config.local.json")
    conn = srv.get_db()
    row = job_repo.get_job(conn, body.job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    jd = srv._job_dict_for_resume(row)
    d = srv._masters()
    name = srv.resume_schema.best_profile_for_job(d, jd)[0] if body.auto \
        else (body.profile or d.get("default") or "").strip()
    try:
        doc = srv.resume_schema.get_profile(d, name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown profile {e.args[0]!r}")
    identity = srv.resume_render.to_txt(doc)
    sources = srv._cfg.get("resume_sources") or [str(srv.BASE_RESUMES)]  # W2.4: repo dir, not a personal path
    corpus = resume_digest.digest(sources)
    if corpus["chars"] < 200:
        raise HTTPException(400, "No readable resume source files found (set resume_sources in config.local.json)")
    import time as _time
    import uuid as _uuid
    srv.BUILT_DIR.mkdir(exist_ok=True)
    base = f"{re.sub(r'[^A-Za-z0-9-]+', '_', row['title'] or 'cv')[:36]}_{_time.strftime('%Y%m%d-%H%M%S')}_{_uuid.uuid4().hex[:6]}"
    out_dir = srv.BUILT_DIR / f"{base}_work"
    res = resume_yamlcv.build_one_pager(
        llm, resume_digest.corpus_text(corpus), identity, srv.job_brief(jd), out_dir)
    final = srv.BUILT_DIR / f"{base}.pdf"
    if res["pdf"]:
        Path(res["pdf"]).replace(final)
    ym = srv.BUILT_DIR / f"{base}.yaml"
    ym.write_text(res["yaml"], encoding="utf-8")
    if not res["ok"]:
        last = res["history"][-1] if res.get("history") else {}
        tail = f" (last attempt: {last.get('pages', '?')} pages)" if last.get("pages") else ""
        raise HTTPException(422, f"{res.get('error') or 'could not fit one page'}{tail}")
    return {
        "profile": name,
        "ok": True,
        "rounds": res["rounds"],
        "name": final.name,
        "url": f"/api/resume/built/{final.name}",
        "yaml": res["yaml"],
        "history": res["history"],
    }
