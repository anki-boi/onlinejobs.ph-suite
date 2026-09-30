"""app/routers/cv.py — the one-page CV build, streamed (B12).

Split out of resume.py (which was at the 250-line cap) and turned into SSE: the
build is the long one — LLM draft, render, page-check, fix, up to four rounds —
and it used to be a single blocking POST with no progress and no way to stop.
Now each round is a `log` event and the Stop button flips the run's token.
"""

import re
import time as _time
import uuid as _uuid
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter

from app import scheduler
from app.sse import hardened, sse
from app import server as srv
from app.schemas import TailorRequest
from db.repos import jobs as job_repo

router = APIRouter(tags=['Resume'])


@router.post("/api/resume/build")
def resume_build(body: TailorRequest, request: Request):
    """Build a 1-page CV for one job, streaming each round (B12)."""
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
    # W2.4: default to the repo's own resumes/ dir, not someone's personal path
    sources = srv._cfg.get("resume_sources") or [str(srv.BASE_RESUMES)]
    corpus = resume_digest.digest(sources)
    if corpus["chars"] < 200:
        raise HTTPException(400, "No readable resume source files found (set resume_sources in config.local.json)")

    run_id = request.headers.get("x-run-id") or f"build-{_uuid.uuid4().hex[:8]}"
    token = scheduler.StopToken()
    scheduler.register_run(run_id, token)

    def generate():
        srv.BUILT_DIR.mkdir(exist_ok=True)
        base = (f"{re.sub(r'[^A-Za-z0-9-]+', '_', row['title'] or 'cv')[:36]}"
                f"_{_time.strftime('%Y%m%d-%H%M%S')}_{_uuid.uuid4().hex[:6]}")
        out_dir = srv.BUILT_DIR / f"{base}_work"
        res = None
        try:
            for kind, payload in resume_yamlcv.iter_one_pager(
                    llm, resume_digest.corpus_text(corpus), identity, srv.job_brief(jd),
                    out_dir, should_stop=lambda: token.stopped):
                if kind == "result":
                    res = payload
                else:
                    yield sse("log", payload)
        finally:
            # B12: the run stops existing the moment the stream does, or
            # scheduler.wait_runs() would block on a finished build forever.
            scheduler.end_run(run_id)
        final = srv.BUILT_DIR / f"{base}.pdf"
        if res and res.get("pdf"):
            Path(res["pdf"]).replace(final)
        ym = srv.BUILT_DIR / f"{base}.yaml"
        ym.write_text((res or {}).get("yaml", ""), encoding="utf-8")
        if res and res.get("stopped"):
            # A stopped build keeps its YAML draft — the rounds so far were real.
            yield sse("done", {"stopped": True, "rounds": res.get("rounds", 0),
                               "yaml": res.get("yaml", ""), "name": ym.name,
                               "url": f"/api/resume/built/{ym.name}"})
            return
        if not res or not res.get("ok"):
            last = (res or {}).get("history", [{}])[-1] if (res or {}).get("history") else {}
            tail = f" (last attempt: {last.get('pages', '?')} pages)" if last.get("pages") else ""
            raise HTTPException(422, f"{(res or {}).get('error') or 'could not fit one page'}{tail}")
        yield sse("done", {"profile": name, "ok": True, "rounds": res["rounds"],
                           "name": final.name, "url": f"/api/resume/built/{final.name}",
                           "yaml": res["yaml"], "history": res["history"]})

    return StreamingResponse(hardened(generate(), "resume/build", run_id),
                             media_type="text/event-stream")