"""
db/repos/skills.py — Skill tag catalogue operations.
"""

import sqlite3


def upsert_skills(conn: sqlite3.Connection, skills: list[dict]) -> int:
    """
    Bulk upsert skill tags from the API.
    Each skill dict: {id, name, parent_id, slug, category_path}
    Returns count upserted.
    """
    conn.executemany(
        """INSERT INTO skill_tags (id, name, parent_id, slug, category_path)
           VALUES (:id, :name, :parent_id, :slug, :category_path)
           ON CONFLICT(id) DO UPDATE SET
             name = excluded.name,
             parent_id = excluded.parent_id,
             slug = excluded.slug,
             category_path = excluded.category_path""",
        skills,
    )
    conn.commit()
    return len(skills)


def get_all_skills(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM skill_tags ORDER BY category_path, name").fetchall()


def search_skills(conn: sqlite3.Connection, keyword: str) -> list[sqlite3.Row]:
    like = f"%{keyword}%"
    return conn.execute(
        "SELECT * FROM skill_tags WHERE name LIKE ? OR category_path LIKE ? ORDER BY name",
        (like, like),
    ).fetchall()


def get_categories(conn: sqlite3.Connection) -> list[dict]:
    """Distinct top-level categories with counts AND the slug OJ.ph actually uses.

    B5 (audit): this used to return {name, count} only, so the browser invented a
    slug (`name.toLowerCase().replace(/ /g,'-')`) and interpolated it raw into
    /jobseekers/search/c/{slug}/{offset} — "Hosting & Infrastructure Management"
    became a path with a literal `&` in it, and the scrape hit nothing. The slug
    is already in skill_tags (children carry their parent's slug); use the most
    common one per category.
    """
    from collections import Counter
    rows = conn.execute(
        "SELECT category_path, slug FROM skill_tags WHERE category_path != ''"
    ).fetchall()
    counts: dict[str, int] = {}
    slugs: dict[str, Counter] = {}
    for r in rows:
        top = r["category_path"].split(" > ")[0]
        counts[top] = counts.get(top, 0) + 1
        if r["slug"]:
            slugs.setdefault(top, Counter())[r["slug"]] += 1
    out = []
    for name, count in sorted(counts.items()):
        pick = slugs.get(name)
        out.append({"name": name, "count": count,
                    "slug": (pick.most_common(1)[0][0] if pick else "") or _slugify(name)})
    return out


def _slugify(name: str) -> str:
    """Last-resort slug for a category with no tagged children to learn from."""
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s
