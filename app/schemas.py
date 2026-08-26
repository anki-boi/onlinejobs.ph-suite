"""
app/schemas.py — Pydantic request/response models.
"""

from datetime import date
from pydantic import BaseModel


# ── Pipeline ────────────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    keyword: str = ""
    category: str | None = None
    posted_since: date | None = None


class CheckRequest(BaseModel):
    workers: int = 3
    recheck_all: bool = False
    max_age_days: int = 7


# ── Keywords ────────────────────────────────────────────────────────────────

class KeywordFilter(BaseModel):
    positive: list[str] = []
    negative: list[str] = []


# ── Job updates ─────────────────────────────────────────────────────────────

class StatusUpdate(BaseModel):
    status: str


class NotesUpdate(BaseModel):
    notes: str


class FollowUpUpdate(BaseModel):
    follow_up: str
