"""
scraper/pipeline.py — Two-phase pipeline: harvest + enrich.

Phase 1 (harvest): Scrape search result pages → JobStub records.
Phase 2 (enrich):  Fetch detail pages for new/unenriched jobs → JobDetail.

Both phases are generators that yield PipelineEvent objects.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Generator
from urllib.parse import quote_plus

from scraper.client import OJClient, ScrapeStopped, RateLimitExhausted
from scraper.parsers import (
    JobDetail,
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
    skill_ids: list[int] | None = None,
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

    # skill_tags takes comma-separated numeric skill IDs
    skill_tags_str = ",".join(str(i) for i in (skill_ids or []))

    params = [
        f"jobkeyword={quote_plus(keyword)}",
        f"skill_tags={quote_plus(skill_tags_str)}",
        f"gig={'on' if gig else 'off'}",
        f"partTime={'on' if part_time else 'off'}",
        f"fullTime={'on' if full_time else 'off'}",
        "isFromJobsearchForm=1",
    ]
    return f"{base}?{'&'.join(params)}"


def harvest(
    client: OJClient,
    keyword: str = "",
    categories: list[str] | None = None,
    skill_ids: list[int] | None = None,
    posted_since: str | None = None,
    existing_ids: set[int] | None = None,
) -> Generator[PipelineEvent, None, None]:
    """
    Phase 1: scrape search result pages.
    
    If `categories` is provided, search within each category.
    Otherwise use the global keyword search.
    `skill_ids` are passed to OJ.ph's skill_tags URL parameter.
    `keyword` is used as a client-side filter on results.
    """
    existing_ids = existing_ids or set()
    keyword = (keyword or "").strip().lower()
    total_new = 0
    total_seen = 0

    # Determine which URLs to search
    if categories:
        yield PipelineEvent("log", f"Harvest: {len(categories)} cats, kw={keyword!r}, skills={len(skill_ids or [])}")
        base_targets = [(cat, cat) for cat in categories]
    else:
        yield PipelineEvent("log", f"Harvest: kw={keyword!r}, skills={len(skill_ids or [])}")
        base_targets = [("all", None)]

    # OJ.ph ANDs a comma-separated skill_tags value (multiple skills in one
    # query -> almost always zero results), so run one search per skill and
    # union the results — OR semantics, which is what "pick skills" means.
    skill_sets = [[s] for s in (skill_ids or [])] or [None]
    search_targets = []
    for label, slug in base_targets:
        for ss in skill_sets:
            search_targets.append((f"{label} / skill {ss[0]}" if ss else label, slug, ss))

    emitted_ids: set[int] = set()  # dedupe across per-skill searches

    for label, slug, target_skills in search_targets:
        page = 0
        expected_total = None

        while True:
            if client.stopped:
                yield PipelineEvent("log", "[STOPPED]")
                break

            url = search_url(client.base_url, keyword, page, category=slug, skill_ids=target_skills)
            yield PipelineEvent("log", f"  [{label}] page {page + 1}")

            try:
                resp = client.get(url)
            except ScrapeStopped:
                yield PipelineEvent("log", "[STOPPED]")
                break
            except RateLimitExhausted as exc:
                yield PipelineEvent("error", f"Rate limit: {exc}")
                break
            except Exception as exc:
                yield PipelineEvent("error", f"Fetch error: {exc}")
                break

            if page == 0:
                expected_total = get_total_results(resp.text)
                if expected_total:
                    yield PipelineEvent("log", f"    total: {expected_total}")

            stubs = parse_search_results(resp.text)
            if not stubs:
                if page == 0 and expected_total:
                    # The site claims results but our selectors matched nothing —
                    # almost certainly a markup change, not an empty board.
                    yield PipelineEvent(
                        "error",
                        f"⚠ [{label}] site claims {expected_total} results but 0 job boxes "
                        f"parsed — the site structure may have changed",
                    )
                break

            new_stubs = []
            for stub in stubs:
                total_seen += 1
                if stub.job_id and (stub.job_id in existing_ids or stub.job_id in emitted_ids):
                    continue
                # Keyword filter (client-side): title must contain any keyword
                if keyword:
                    title_lower = (stub.title or "").lower()
                    if not any(kw in title_lower for kw in (k.strip() for k in keyword.split(","))):
                        continue
                if posted_since and stub.posted_date and stub.posted_date < posted_since:
                    continue
                new_stubs.append(stub)

            if new_stubs:
                emitted_ids.update(s.job_id for s in new_stubs if s.job_id)
                yield PipelineEvent(
                    "harvest_result",
                    f"  [{label}] page {page + 1}: {len(new_stubs)} new",
                    {"stubs": [
                        {
                            "job_id": s.job_id, "job_url": s.job_url,
                            "title": s.title, "work_type": s.work_type,
                            "company": s.company, "posted_date": s.posted_date,
                            "salary": s.salary, "location": s.location,
                            "hours": s.hours, "skills": s.skills,
                        } for s in new_stubs
                    ], "keyword": keyword, "category": slug},
                )
                total_new += len(new_stubs)

            # Stop conditions
            if expected_total and total_seen >= expected_total:
                break
            if page > 0 and not new_stubs:
                break
            page += 1

    yield PipelineEvent(
        "summary",
        f"Harvest done: {total_new} new / {total_seen} seen",
        {"new": total_new, "seen": total_seen, "stopped": client.stopped},
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
    fetched_200 = 0   # non-gone pages that came back successfully
    parsed_titles = 0 # of those, pages where a title actually parsed

    def _fetch_one(row_id: int, url: str) -> tuple[int, JobDetail | None, str | None]:
        try:
            if client.stopped:
                return row_id, None, "stopped"
            resp = client.get(url)
            if resp.status_code in (404, 410):
                # 404 = removed, 410 = permanently deleted ("Job No Longer Posted").
                # Both mean the posting is gone → Closed.
                reason = f"{resp.status_code} — job removed from site"
                return row_id, JobDetail(job_url=url, is_closed=True, close_reason=reason), None
            detail = parse_job_detail(resp.text, url=url)
            return row_id, detail, None
        except ScrapeStopped:
            return row_id, None, "stopped"
        except Exception as exc:
            return row_id, None, str(exc)

    done = 0
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich")
    try:
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
                                   {"row_id": row_id, "error": err,
                                    "progress": f"{done}/{len(jobs)}"})
                continue

            if detail is None:
                continue

            if detail.is_closed:
                closed_count += 1
                icon = "🔴"
            else:
                fetched_200 += 1
                if detail.title:
                    parsed_titles += 1
                open_count += 1
                icon = "🟢"

            filled = [k for k in ("title", "company", "description", "salary", "skills")
                      if getattr(detail, k, None)]
            note = f"  ✅ {', '.join(filled)}" if filled else "  ⚠ no details"
            yield PipelineEvent(
                "enrich_result",
                f"[{done}/{len(jobs)}] {icon} {'Closed' if detail.is_closed else 'Open'} {url}{note}",
                {
                    "progress": f"{done}/{len(jobs)}",
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
    finally:
        # The context-manager form (with ThreadPoolExecutor) crashes when this
        # generator is closed from one of the pool's own threads — CPython's gc
        # can reclaim the generator frame on any thread, and join() then raises
        # "cannot join current thread" (seen in server_e2e.log on client
        # disconnect). wait=False + cancel_futures is safe from every thread;
        # in-flight requests run to their natural end and the workers exit.
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except RuntimeError:
            pool.shutdown(wait=False, cancel_futures=True)

    if not client.stopped and fetched_200 and not parsed_titles:
        yield PipelineEvent(
            "error",
            f"⚠ {fetched_200} detail page(s) fetched but none parsed a title — "
            f"the site structure may have changed",
        )

    yield PipelineEvent(
        "summary",
        f"Enrich complete: 🟢 {open_count} open, 🔴 {closed_count} closed, ⚠ {error_count} errors",
        {"open": open_count, "closed": closed_count, "errors": error_count,
         "total": len(jobs), "stopped": client.stopped},
    )
