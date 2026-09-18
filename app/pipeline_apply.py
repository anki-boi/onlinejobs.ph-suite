"""
app/pipeline_apply.py — apply pipeline events to the DB.

Shared by the manual pipeline endpoints (app/server.py) and the auto-run
scheduler (app/scheduler.py) so the two paths never drift apart.
"""

import json

from db.repos import jobs as job_repo


def apply_harvest(conn, event) -> tuple[int, list[tuple[dict, int]]]:
    """Upsert one harvest_result event's stubs.

    Returns (inserted_count, new_items) where each new_item is (stub_dict, row_id)
    for the freshly-inserted rows, in order.
    """
    inserted = 0
    new_items: list[tuple[dict, int]] = []
    for stub in event.data.get("stubs", []):
        row_id, is_new = job_repo.upsert_stub(
            conn,
            job_id=stub.get("job_id") or 0,
            job_url=stub["job_url"],
            title=stub.get("title"),
            work_type=stub.get("work_type"),
            company=stub.get("company"),
            posted_date=stub.get("posted_date"),
            salary=stub.get("salary"),
            location=stub.get("location"),
            hours=stub.get("hours"),
            skills=stub.get("skills") or None,
            search_keyword=event.data.get("keyword") or None,
            search_category=event.data.get("category"),
        )
        if is_new:
            inserted += 1
            new_items.append((stub, row_id))
    return inserted, new_items


def apply_saved_keyword_rules(conn) -> tuple[int, int, int]:
    """Re-apply the saved positive/negative keyword rules after enrichment,
    so rules are judged on the full description, not the pre-enrich stub.
    Mirrors the auto-run (app/scheduler.py). Returns (neg, pos, restored)."""
    from db.repos import settings as settings_repo
    pos = json.loads(settings_repo.get(conn, "positive_keywords", "[]") or "[]")
    neg = json.loads(settings_repo.get(conn, "negative_keywords", "[]") or "[]")
    if not (pos or neg):
        return 0, 0, 0
    from app.services.keywords import apply_keyword_filters
    return apply_keyword_filters(conn, pos, neg)


def apply_enrich(conn, d: dict) -> bool:
    """Apply one enrich_result payload. Returns False for errors (nothing written)."""
    if "error" in d:
        return False
    job_repo.enrich_job(
        conn, d["row_id"],
        title=d.get("title"),
        company=d.get("company"),
        description=d.get("description"),
        salary=d.get("salary"),
        hours_per_week=d.get("hours_per_week"),
        work_type=d.get("work_type"),
        date_updated=d.get("date_updated"),
        skills=d.get("skills") or None,
        employer_id=d.get("employer_id"),
        is_closed=d.get("is_closed", False),
        close_reason=d.get("close_reason"),
    )
    return True
