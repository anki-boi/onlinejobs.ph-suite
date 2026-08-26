"""
scraper/skills.py — Skills API client.

The skills API is a proper JSON endpoint:
    POST https://api.onlinejobs.ph/api/v1/skills/search
    Body: {"keyword": "..."}   (empty string = all skills)
    Response: [{id, name, parent: [{id, name, slug, parent_id}], children: [...]}]
"""

import logging

from scraper.client import OJClient

log = logging.getLogger(__name__)


def fetch_skills(client: OJClient, keyword: str = "") -> list[dict]:
    """Fetch skills from the API. Returns flat list of skill dicts."""
    resp = client.post_json(
        f"{client.api_url}/api/v1/skills/search",
        {"keyword": keyword},
    )
    data = resp.json()
    if not isinstance(data, list):
        log.warning("Skills API returned unexpected type: %s", type(data))
        return []
    log.info("Fetched %d skills (keyword=%r)", len(data), keyword)
    return data


def skills_to_db_rows(skills: list[dict]) -> list[dict]:
    """Convert API skill objects to skill_tags table rows."""
    rows: list[dict] = []
    for s in skills:
        parents = s.get("parent") or []
        parent_id = parents[-1]["id"] if parents else None
        category_path = " > ".join(p["name"] for p in parents) if parents else ""
        slug = s.get("slug", "")
        if not slug and parents:
            slug = parents[-1].get("slug", "")
        rows.append({
            "id": s["id"],
            "name": s["name"],
            "parent_id": parent_id,
            "slug": slug,
            "category_path": category_path,
        })
    return rows


def top_level_categories(skills: list[dict]) -> list[dict]:
    """Extract distinct top-level categories with counts."""
    cats: dict[str, int] = {}
    for s in skills:
        parents = s.get("parent") or []
        top = parents[0]["name"] if parents else "Uncategorized"
        cats[top] = cats.get(top, 0) + 1
    return [
        {"name": k, "count": v, "slug": _cat_slug(skills, k)}
        for k, v in sorted(cats.items())
    ]


def _cat_slug(skills: list[dict], category_name: str) -> str:
    """Find the slug for a top-level category."""
    for s in skills:
        parents = s.get("parent") or []
        if parents and parents[0]["name"] == category_name:
            return parents[0].get("slug", "")
    return ""
