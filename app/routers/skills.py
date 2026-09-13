"""app/routers/skills.py — skill tags listing/refresh. (W5.1 split; no
behaviour change.)"""

from fastapi import HTTPException
from fastapi.routing import APIRouter
from scraper.client import ScrapeStopped
from scraper.skills import fetch_skills, skills_to_db_rows
from db.repos import skills as skill_repo
from app.server import get_client, get_db

router = APIRouter()


@router.get("/api/skills")
def list_skills(search: str | None = None):
    conn = get_db()
    if search:
        rows = skill_repo.search_skills(conn, search)
    else:
        rows = skill_repo.get_all_skills(conn)
    return [dict(r) for r in rows]


@router.get("/api/skills/categories")
def skill_categories():
    conn = get_db()
    return skill_repo.get_categories(conn)


@router.post("/api/skills/refresh")
def refresh_skills():
    client = get_client()
    try:
        skills = fetch_skills(client, keyword="")
    except ScrapeStopped:
        raise  # user-initiated stop → 409 envelope, not a 502 upstream error
    except Exception as exc:
        raise HTTPException(502, f"Skills API error: {exc}")
    rows = skills_to_db_rows(skills)
    conn = get_db()
    count = skill_repo.upsert_skills(conn, rows)
    return {"count": count}
