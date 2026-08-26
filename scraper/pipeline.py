"""
scraper/pipeline.py — Two-phase pipeline: harvest + enrich.

Phase 1 (harvest): Scrape search result pages → JobStub records.
Phase 2 (enrich):  Fetch detail pages for new/unenriched jobs → JobDetail.

Both phases are generators that yield PipelineEvent objects.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Generator
from urllib.parse import quote_plus

from scraper.client import OJClient, ScrapeStopped, RateLimitExhausted
from scraper.parsers import (
    JobDetail,
    JobStub,
    get_total_results,
    parse_job_detail,
    parse_search_results,
)

log = logging.getLogger(__name__)

JOBS_PER_PAGE = 30


@dataclass
class PipelineEvent:
    type: str          # "log", "harvest_result", "enrich_result", "summary", "error", "done"
    message: str = ""
    data: dict = field(default_factory=dict)


# ── Phase 1: Harvest ────────────────────────────────────────────────────────

def search_url(
    base_url: str,
    keyword: str,
    page: int,
    gig: bool = True,
    part_time: bool = True,
    full_time: bool = True,
    category: str | None = None,
) -> str:
    """Build a search URL.

    If `category` is given, uses the category search path.
    Otherwise uses the keyword search path with pagination.
    """
    if category:
        return f"{base_url}/jobseekers/search/c/{category}/{page * JOBS_PER_PAGE}"

    offset = page * JOBS_PER_PAGE
    base = f"{base_url}/jobseekers/jobsearch"
    if offset > 0:
        base += f"/{offset}"
    params = [
        f"jobkeyword={quote_plus(keyword)}",
        "skill_tags=",
        f"gig={'on' if gig else 'off'}",
        f"partTime={'on' if part_time else 'off'}",
        f"fullTime={'on' if full_time else 'off'}",
        "isFromJobsearchForm=1",
    ]
    return f"{base}?{'&'.join(params)}"


def harvest(
    client: OJClient,
    keyword: str = "",
    category: str | None = None,
    posted_since: str | None = None,
    existing_ids: set[int] | None = None,
) -> Generator[PipelineEvent, None, None]:
    """
    Phase 1: scrape search result pages.
    Yields PipelineEvents as it goes.
    """
    existing_ids = existing_ids or set()
    keyword = (keyword or "").strip()
    page = 0
    total_new = 0
    total_seen = 0
    expected_total: int | None = None

    yield PipelineEvent("log", f"Starting harvest: keyword={keyword!r}, category={category!r}")

    while True:
        if client.stopped:
            yield PipelineEvent("log", "⛔ Stopped by user")
            break

        url = search_url(client.base_url, keyword, page, category=category)
        yield PipelineEvent("log", f"  Page {page + 1} → {url}")

        try:
            resp = client.get(url)
        except ScrapeStopped:
            yield PipelineEvent("log", "⛔ Stopped")
            break
        except RateLimitExhausted as exc:
            yield PipelineEvent("error", f"Rate limit exhausted: {exc}")
            break
        except Exception as exc:
            yield PipelineEvent("error", f"Failed to fetch page {page + 1}: {exc}")
            break

        # Check total on first page
        if page == 0:
            expected_total = get_total_results(resp.text)
            if expected_total:
                yield PipelineEvent("log", f"  Total results: {expected_total}")

        stubs = parse_search_results(resp.text)
        if not stubs:
            yield PipelineEvent("log", f"  No jobs on page {page + 1} — stopping")
            break

        new_stubs: list[JobStub] = []
        oldest_date = None

        for stub in stubs:
            total_seen += 1
            if stub.job_id and stub.job_id in existing_ids:
                continue

            # posted_since filter
            if posted_since and stub.posted_date:
                if stub.posted_date < posted_since:
                    oldest_date = stub.posted_date
                    continue

            new_stubs.append(stub)

        yield PipelineEvent(
            "harvest_result",
            f"  Page {page + 1}: {len(stubs)} seen, {len(new_stubs)} new",
            {"stubs": [
                {
                    "job_id": s.job_id,
                    "job_url": s.job_url,
                    "title": s.title,
                    "work_type": s.work_type,
                    "company": s.company,
                    "posted_date": s.posted_date,
                    "salary": s.salary,
                    "location": s.location,
                    "hours": s.hours,
                    "skills": s.skills,
                }
                for s in new_stubs
            ], "keyword": keyword, "category": category},
        )
        total_new += len(new_stubs)

        # Stop conditions
        if posted_since and oldest_date and oldest_date < posted_since:
            yield PipelineEvent("log", f"  Reached jobs older than {posted_since} — stopping")
            break

        # Check if we've seen all expected results
        if expected_total and total_seen >= expected_total:
            yield PipelineEvent("log", f"  All {expected_total} results processed")
            break

        # No new stubs and we're past page 1 — probably all duplicates
        if page > 0 and not new_stubs:
            yield PipelineEvent("log", f"  No new jobs on page {page + 1} — stopping")
            break

        page += 1
        time.sleep(0.5)  # small pause between pages (the client already throttles)

    yield PipelineEvent(
        "summary",
        f"Harvest complete: {total_new} new, {total_seen} total seen",
        {"new": total_new, "seen": total_seen, "expected": expected_total},
    )


# ── Phase 2: Enrich ─────────────────────────────────────────────────────────

def enrich(
    client: OJClient,
    jobs: list[tuple[int, str]],
    workers: int = 3,
) -> Generator[PipelineEvent, None, None]:
    """
    Phase 2: fetch detail pages for jobs.
    `jobs` is a list of (row_id, job_url).
    Uses ThreadPoolExecutor with a shared OJClient (rate-limited).
    """
    if not jobs:
        yield PipelineEvent("log", "No jobs to enrich")
        return

    yield PipelineEvent("log", f"Enriching {len(jobs)} job(s) with {workers} worker(s)…")

    open_count = 0
    closed_count = 0
    error_count = 0

    def _fetch_one(row_id: int, url: str) -> tuple[int, JobDetail | None, str | None]:
        try:
            if client.stopped:
                return row_id, None, "stopped"
            resp = client.get(url)
            if resp.status_code == 404:
                return row_id, JobDetail(job_url=url, is_closed=True, close_reason="404 Not Found"), None
            detail = parse_job_detail(resp.text, url=url)
            return row_id, detail, None
        except ScrapeStopped:
            return row_id, None, "stopped"
        except Exception as exc:
            return row_id, None, str(exc)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, rid, url): (rid, url) for rid, url in jobs}

        for future in as_completed(futures):
            rid, url = futures[future]
            done += 1
            row_id, detail, err = future.result()

            if err == "stopped":
                yield PipelineEvent("log", "⛔ Stopped during enrich")
                break
            if err:
                error_count += 1
                yield PipelineEvent("enrich_result", f"[{done}/{len(jobs)}] ⚠ {url} — {err}",
                                   {"row_id": row_id, "error": err})
                continue

            if detail is None:
                continue

            if detail.is_closed:
                closed_count += 1
                icon = "🔴"
            else:
                open_count += 1
                icon = "🟢"

            filled = [k for k in ("title", "company", "description", "salary", "skills")
                      if getattr(detail, k, None)]
            note = f"  ✅ {', '.join(filled)}" if filled else "  ⚠ no details"
            yield PipelineEvent(
                "enrich_result",
                f"[{done}/{len(jobs)}] {icon} {'Closed' if detail.is_closed else 'Open'} {url}{note}",
                {
                    "row_id": row_id,
                    "job_id": detail.job_id,
                    "title": detail.title,
                    "company": detail.company,
                    "description": detail.description,
                    "work_type": detail.work_type,
                    "salary": detail.salary,
                    "hours_per_week": detail.hours_per_week,
                    "date_updated": detail.date_updated,
                    "skills": detail.skills,
                    "employer_id": detail.employer_id,
                    "is_closed": detail.is_closed,
                    "close_reason": detail.close_reason,
                },
            )

    yield PipelineEvent(
        "summary",
        f"Enrich complete: 🟢 {open_count} open, 🔴 {closed_count} closed, ⚠ {error_count} errors",
        {"open": open_count, "closed": closed_count, "errors": error_count, "total": len(jobs)},
    )
