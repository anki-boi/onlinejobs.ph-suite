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
    """Distinct top-level categories with counts."""
    rows = conn.execute(
        "SELECT category_path, COUNT(*) as cnt FROM skill_tags "
        "WHERE category_path != '' GROUP BY split(category_path, ' > ', 1) ORDER BY 1"
    ).fetchall() if False else []
    # SQLite doesn't have split(); do it in Python
    all_rows = conn.execute("SELECT category_path FROM skill_tags WHERE category_path != ''").fetchall()
    cats: dict[str, int] = {}
    for r in all_rows:
        top = r["category_path"].split(" > ")[0] if r["category_path"] else "Uncategorized"
        cats[top] = cats.get(top, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(cats.items())]
