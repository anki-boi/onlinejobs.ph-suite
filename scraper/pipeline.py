"""
scraper/pipeline.py — Two-phase pipeline: harvest + enrich.

Phase 1 (harvest): Scrape search result pages → JobStub records.
Phase 2 (enrich):  Fetch detail pages for new/unenriched jobs → JobDetail.

Both phases are generators that yield PipelineEvent objects.
"""

import logging
import re
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

    B5 (audit): the category path is percent-encoded. Category names like
    "Hosting & Infrastructure Management" used to be interpolated raw, so the `&`
    ended the path segment and the request hit a different page entirely.
    """
    if category:
        return f"{base_url}/jobseekers/search/c/{quote_plus(str(category))}/{page * JOBS_PER_PAGE}"

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


def split_keywords(keyword) -> list[str]:
    """B1 (audit): "data entry, VA" was sent to OJ.ph as ONE jobkeyword, i.e. the
    literal phrase "data entry, VA" — which matches nothing. Chips are OR'd, so
    each keyword gets its own search (the same rule already applied to skills).
    Accepts a comma/semicolon-separated string or a list."""
    if not keyword:
        return []
    raw = list(keyword) if isinstance(keyword, (list, tuple)) else re.split(r"[,;]", str(keyword))
    out: list[str] = []
    for k in raw:
        k = str(k).strip()
        if k and k.lower() not in [x.lower() for x in out]:
            out.append(k)
    return out


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

    The search space is the product of the scopes the user picked: one search per
    keyword × per category × per skill (all OR'd — OJ.ph ANDs a comma-separated
    skill_tags value, and a multi-word keyword string is a phrase, not a list).
    `existing_ids` dedupes against the DB; `emitted_ids` against this run.
    """
    existing_ids = existing_ids or set()
    kws = split_keywords(keyword)
    cats = [c for c in (categories or []) if c]
    total_new = 0
    total_seen = 0

    yield PipelineEvent(
        "log",
        f"Harvest: kw={kws or ['(all)']} cats={cats or ['(all)']} "
        f"skills={len(skill_ids or [])}",
    )

    # OJ.ph ANDs a comma-separated skill_tags value (multiple skills in one
    # query -> almost always zero results), so run one search per skill and
    # union the results — OR semantics, which is what "pick skills" means.
    skill_sets = [[s] for s in (skill_ids or [])] or [None]
    search_targets = []
    for kw in (kws or [""]):
        for slug in (cats or [None]):
            for ss in skill_sets:
                parts = []
                if kw:
                    parts.append(f"kw:{kw}")
                if slug:
                    parts.append(f"cat:{slug}")
                if ss:
                    parts.append(f"skill {ss[0]}")
                label = " / ".join(parts) or "all"
                search_targets.append((label, kw, slug, ss))

    if len(search_targets) > 1:
        yield PipelineEvent("log", f"  {len(search_targets)} searches (1 request each, throttled)")

    emitted_ids: set[int] = set()  # dedupe across every search in this run

    for label, target_kw, slug, target_skills in search_targets:
        page = 0
        expected_total = None
        seen_here = 0        # B8: per-target counters — `total_seen` across all
        new_here = 0         # targets over-counted against a per-query total and
                             # stopped pagination early, skipping whole pages

        while True:
            if client.stopped:
                yield PipelineEvent("log", "[STOPPED]")
                break

            url = search_url(client.base_url, target_kw, page, category=slug, skill_ids=target_skills)
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
                seen_here += 1
                total_seen += 1
                if stub.job_id and (stub.job_id in existing_ids or stub.job_id in emitted_ids):
                    continue
                # B2 (audit, D1): no client-side title filter. The site already
                # searched the keyword; requiring it in the title threw away jobs
                # like "Part-time accountant" from a `bookkeeper` search. The
                # post-hoc rule for that is the auto-hide keyword panel.
                if posted_since and stub.posted_date and stub.posted_date < posted_since:
                    continue
                new_stubs.append(stub)

            if new_stubs:
                emitted_ids.update(s.job_id for s in new_stubs if s.job_id)
                new_here += len(new_stubs)
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
                            "category": s.category,
                        } for s in new_stubs
                    ], "keyword": target_kw, "category": slug},
                )
                total_new += len(new_stubs)

            # Stop conditions (B8: measured against THIS search's own total)
            if expected_total and seen_here >= expected_total:
                break
            # X-B (F3, the extension's `pastHorizon`): the board is sorted
            # newest-first — verified against both live fixtures — so the OLDEST card
            # on this page is the tail of the list. Once it is past the recency window,
            # every later page is too, and paging them is Cloudflare exposure bought for
            # listings the window would drop anyway. A page with no readable dates never
            # ends the list: the rule that must not lose a listing does not get to
            # decide when the list ends.
            dates = [s.posted_date for s in stubs if s.posted_date]
            if posted_since and dates and min(dates) < posted_since:
                yield PipelineEvent(
                    "log",
                    f"  [{label}] recency window ends here (oldest {min(dates)[:10]}) "
                    f"— not paging further",
                )
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
    rate_limited = 0   # L6: how many fetches gave up on 429/52x
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
        except RateLimitExhausted as exc:
            # L6: marked so the storm guard below can see it is a rate limit and
            # not a bad page.
            return row_id, None, f"rate-limited (HTTP {exc.status})"
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
                if err.startswith("rate-limited"):
                    rate_limited += 1
                    # L6: a live run lost minutes to HTTP 521 — the site's edge was
                    # refusing us, and the pipeline kept grinding through hundreds of
                    # jobs. Once most of what we've tried is a rate limit, stop and
                    # leave the rest pending; the next run picks them up.
                    if done >= 8 and rate_limited >= max(5, done // 2):
                        yield PipelineEvent(
                            "error",
                            f"Site is rate-limiting ({rate_limited}/{done} fetches) — "
                            f"stopping enrich; {len(jobs) - done} job(s) stay queued "
                            f"for the next run",
                            {"rate_limited": rate_limited, "remaining": len(jobs) - done},
                        )
                        break
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
        f"Enrich complete: 🟢 {open_count} open, 🔴 {closed_count} closed, ⚠ {error_count} errors"
        + (f", rate-limited {rate_limited}" if rate_limited else ""),
        {"open": open_count, "closed": closed_count, "errors": error_count,
         "rate_limited": rate_limited,
         "total": len(jobs), "stopped": client.stopped},
    )
