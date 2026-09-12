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


class ScrapeScope(BaseModel):
    """What the auto-run harvests. All empty = scrape everything (today's behavior)."""
    keyword: str = ""
    categories: list[str] = []
    skills: list[str] = []


class StopRequest(BaseModel):
    """W2.8: optional — target a specific run; omitted = the active run."""
    run_id: str | None = None


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


class ResumeUpdate(BaseModel):
    master: dict
    profile: str = ""


class TailorRequest(BaseModel):
    job_id: int
    profile: str = ""
    auto: int = 0
