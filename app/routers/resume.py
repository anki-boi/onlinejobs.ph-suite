"""app/routers/resume.py — resume profiles, ATS scoring, LLM tailoring,
export, rendercv build. (W5.1 split; no behaviour change. All shared state is
read through the app.server module at call time so test monkeypatches keep
working.)"""

import re

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.routing import APIRouter
from pydantic import BaseModel
from app.schemas import ResumeUpdate, TailorRequest
from app import server as srv
from db.repos import jobs as job_repo
from db.repos import tailored as tailored_repo

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
    # B10: `report` carries why a tailoring was rejected, so the UI can say
    # "the model made up an employer, so your master was kept" instead of
    # quietly shipping the guess.
    report: dict = {}
    tailored = srv.tailor(doc, jd, llm, report)
    # B9: store it. The download and the score must be the same document.
    tailored_repo.put(conn, body.job_id, name, tailored,
                      job_repo.get_jobs_version(conn))
    return {
        "profile": name,
        "tailored": tailored,
        "changed": tailored != doc,
        "guard": report.get("guard"),
        "stored": True,
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
        # B9: serve the document that was scored, not a fresh LLM roll. Only if
        # nothing is stored for this (job, profile) does export tailor on the fly
        # — and it stores that result so the next download matches too.
        stored = tailored_repo.get(conn, job_id, name)
        if stored is None:
            llm = srv.LLMClient.from_config(srv._cfg)
            if llm is None:
                raise HTTPException(503, "No LLM configured - add llm_base_url/llm_model to config.local.json")
            m = srv.tailor(m, jd, llm)
            tailored_repo.put(conn, job_id, name, m, job_repo.get_jobs_version(conn))
        else:
            m = stored
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


class FitListing(BaseModel):
    """A listing the dashboard has never seen — the browser extension's view of a job
    page. Only the text a score is actually built from."""
    title: str = ""
    description: str = ""
    salary: str = ""
    company: str = ""
    work_type: str = ""
    skills: list[str] = []
    keywords: str = ""


class FitRequest(BaseModel):
    jobs: list[FitListing] = []


@router.post("/api/resume/fit")
def resume_fit(body: FitRequest):
    """X-E bridge: how well your resume fits listings you are looking at in the
    browser, scored by the same deterministic scorer the table's Fit column uses.

    Batched on purpose: one request per scanned page, not one per card. The profile
    is per listing because the best profile for a video-editing job is not the best
    profile for a nurse-practitioner job.
    """
    if len(body.jobs) > 50:
        raise HTTPException(400, "fit accepts at most 50 listings per request")
    masters = srv._masters()
    out = []
    for j in body.jobs:
        skills = [x for x in (s.strip() for s in j.skills) if x]
        best = srv.resume_schema.best_profile_for_job(masters, {
            "title": j.title, "description": j.description, "salary": j.salary,
            "company": j.company, "work_type": j.work_type,
            "skills": ", ".join(skills)})
        if best is None:
            out.append({"profile": None, "fit": None, "fit_max": 0, "total": None,
                        "hygiene": None})
            continue
        name, sc = best
        # `fit` is skills (40) + keywords (20). A listing the dashboard never harvested
        # has no search keyword, so only half of it is scorable — and a listing with no
        # skill tags either scores nothing at all. Say what was scored instead of
        # reporting 0/60, which reads as "poor fit" when it means "no data".
        fit_max = (40 if skills else 0) + (20 if j.keywords else 0)
        out.append({"profile": name, "fit": sc.get("fit", 0) if fit_max else None,
                    "fit_max": fit_max, "total": sc["total"],
                    "hygiene": sc.get("hygiene", 0)})
    if not out:
        raise HTTPException(400, "send at least one listing")
    return {"count": len(out), "results": out}
