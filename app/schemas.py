"""
app/schemas.py — Pydantic request/response models.
"""

from datetime import date
from pydantic import BaseModel


# ── Pipeline ────────────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    keyword: str = ""
    categories: list[str] = []      # category slugs (OR scope)
    skills: list[str] = []          # skill names (OR scope)
    posted_since: date | None = None


class CheckRequest(BaseModel):
    workers: int | None = None   # None → use config.json enrich_workers
    recheck_all: bool = False
    max_age_days: int = 7


# ── Auto-run ────────────────────────────────────────────────────────────────

class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = None


# ── Keyword filters (post-enrichment) ───────────────────────────────────────

class KeywordFilter(BaseModel):
    positive: list[str] = []
    negative: list[str] = []
    restore: bool = False  # ignore keywords; restore everything hidden by filters


# ── Job updates ─────────────────────────────────────────────────────────────

class StatusUpdate(BaseModel):
    status: str


class NotesUpdate(BaseModel):
    notes: str


class FollowUpUpdate(BaseModel):
    follow_up: str
