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
    # B7: "stale" had two meanings — auto-run used config's enrich_interval_days
    # while the manual button hardcoded 7. None now means "whatever config says".
    max_age_days: int | None = None


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


# ── Job listing filters (B11) ───────────────────────────────────────────────


class JobQuery(BaseModel):
    """Every filter the jobs table exposes, as query params.

    One model, used by both /api/jobs and /api/jobs/export, so the CSV can no
    longer silently ignore the view on screen (B11): the params are literally the
    same object, not two lists that drift apart."""

    page: int = 1
    per_page: int = 50
    # X-E: the site's job id, for a deep link from the browser extension.
    job_id: int | None = None
    status: str | None = None
    search: str | None = None
    include_hidden: bool = False
    work_type: str | None = None
    skill: str | None = None
    skills: str | None = None            # comma-separated OR filter
    categories: str | None = None        # F5: comma-separated category slugs
    scrape_status: str | None = None     # comma-separated, e.g. "Open,Closed"
    sort: str | None = None              # any sortable column, else newest-first
    order: str = "desc"
    title: str | None = None
    company: str | None = None
    salary: str | None = None
    location: str | None = None
    hours: str | None = None
    posted_from: str | None = None       # inclusive start (YYYY-MM-DD) of posted date
    posted_to: str | None = None         # inclusive end
    has_salary: bool = False
    salary_min_monthly: float | None = None   # job's max (PHP/month) >= this
    salary_max_monthly: float | None = None   # job's min (PHP/month) <= this
    salary_currency: str | None = None        # comma-separated, e.g. "USD,PHP"
    min_ats: int = 0                     # best-profile total >= this
    min_fit: int = 0                     # P1: fit (skills+keywords, /60) >= this
    hide_reposts: bool = False           # F2/B11: the table's "Hide reposts" toggle
    include_deleted: bool = False        # P6: show rows a full reset soft-deleted


# ── Keyword filters (post-enrichment) ───────────────────────────────────────

class KeywordFilter(BaseModel):
    positive: list[str] = []
    negative: list[str] = []
    restore: bool = False  # ignore keywords; restore everything hidden by filters
    # B13: empty lists used to mean "wipe the rules and un-hide everything" — a
    # silent footgun for anything driving the API. Say it on purpose now.
    clear_rules: bool = False
    # X-C (F2): PHP/month. A job that matches a Remove keyword but pays at least this
    # stays visible. None = leave the stored goal alone; 0 = turn the goal off.
    pay_goal_monthly: float | None = None


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
