"""
scraper/parsers.py — Pure HTML→dataclass parsers. No I/O; the only module
state is the SELECTORS table and the MISSING_FIELDS counter (W3.2).

Each function takes an HTML string and returns structured data.
This is the testable core of the scraper.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── Selector table (W3.2) ──────────────────────────────────────────────────
# Single source of truth for every CSS selector the parsers use. Each value
# is an ordered FALLBACK CHAIN: the first selector matching wins. Live markup
# (2026-09-13) puts the company name in the logo img's alt, older markup in
# p[data-temp] — the chain covers both. A field whose whole chain misses
# degrades to None and bumps MISSING_FIELDS, so a broken selector is counted
# and logged, never silently swallowed into an empty job.
SELECTORS: dict[str, list[str]] = {
    "search.job_box":   [".jobpost-cat-box.latest-job-post"],
    "search.job_link":  ["a[href*='/jobseekers/job/']"],
    "search.title":     ["h4"],
    # work_type badge (descendant of the title h4; _parse_title_with_badge
    # selects it relative to the h4, hence no h4 prefix in the chain)
    "search.work_type": [".badge"],
    # company: old markup had it in the <p data-temp> text; live markup only
    # has "Posted on …" there, so the company comes from the logo img's alt.
    "search.company":   ["p[data-temp]", "img.jobpost-cat-box-logo", "img"],
    "search.posted":    ["p[data-temp]"],
    "search.salary":    ["dd.col"],
    "search.desc":      [".desc a:not([target])", ".desc"],
    "search.skills":    [".job-tag a"],
    # no bare-<h1> fallback: the soft-404 page's h1 ("Oops, we lost you
    # there") is not a job title and must not be read as one
    "detail.title":     ["h1.job__title"],
    "detail.company":   ["h3.job__logo", "h3"],
    "detail.employer":  ["h3.job__logo img", "h3 img"],
    "detail.desc":      ["p#job-description", ".job-description"],
    "detail.fields":    ["h3.fs-12"],
    "detail.skills":    ["a.card-worker-topskill"],
    "detail.closed":    ["h3.text-warning", ".alert-warning"],
}

# per-process counter of fields lost to selector misses (tests reset via clear)
from collections import Counter
MISSING_FIELDS: Counter = Counter()


def _first(scope, key: str):
    """First element in SELECTORS[key] that matches in `scope` (soup or box).
    Returns the element or None (caller decides how to degrade)."""
    for sel in SELECTORS[key]:
        el = scope.select_one(sel)
        if el is not None:
            return el
    return None


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class JobStub:
    """A job as seen on the search results page (list view)."""
    job_id: int | None = None
    job_url: str = ""
    title: str | None = None
    work_type: str | None = None       # "Part Time", "Full Time", "Gig", "Any"
    company: str | None = None
    posted_date: str | None = None     # "2026-08-26 08:29:32"
    salary: str | None = None
    location: str | None = None
    hours: str | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class JobDetail:
    """A job as seen on the detail page (full enrichment)."""
    job_id: int | None = None
    job_url: str = ""
    title: str | None = None
    company: str | None = None
    description: str | None = None
    work_type: str | None = None
    salary: str | None = None
    hours_per_week: str | None = None
    date_updated: str | None = None
    skills: list[str] = field(default_factory=list)
    employer_id: int | None = None
    is_closed: bool = False
    close_reason: str | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _clean(text: str | None) -> str | None:
    """Collapse whitespace, strip. Return None if empty.
    Also strips escaped-HTML artifacts the site leaves in titles
    (e.g. '<email class=...>' rendered from &lt; in the markup)."""
    if not text:
        return None
    s = re.sub(r"\s+", " ", text).strip()
    # ponytail: strips <word ...> / </word ...> fragments only; a title that
    # legitimately contains '<' followed by a word would be trimmed (never seen)
    s = re.sub(r"</?[a-zA-Z][^>]*>?\s?", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def extract_job_id(url: str) -> int | None:
    """Extract the numeric job ID from a URL slug.
    '/jobseekers/job/digital-social-media-marketing-director-1701214' → 1701214
    """
    if not url:
        return None
    # Normalise: strip scheme, domain, just get the path
    path = url
    if "://" in path:
        parsed = urlparse(path)
        path = parsed.path
    path = path.rstrip("/")
    # Last segment: "slug-12345"
    last = path.split("/")[-1]
    m = re.search(r"-(\d+)$", last)
    return int(m.group(1)) if m else None


def _parse_structured_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the 4 structured fields from the detail page card.
    Structure: <dt><h3 class='fs-12'>LABEL</h3></dt><dd><p class='fs-18'>value</p></dd>
    Returns dict like {"TYPE OF WORK": "Part Time", "WAGE / SALARY": "$5", ...}
    """
    fields: dict[str, str] = {}
    for h3 in soup.select(SELECTORS["detail.fields"][0]):
        label = _clean(h3.get_text())
        if not label:
            continue
        val = None
        # The value is in the next <dd>'s <p>
        # h3 is inside <dt>, value is in sibling <dd>
        dt = h3.find_parent("dt")
        if dt:
            dd = dt.find_next_sibling("dd")
            if dd:
                p = dd.find("p")
                val = _clean(p.get_text()) if p else None
        if val is None:
            # Fallback: next sibling p
            val_el = h3.find_next_sibling("p")
            if val_el:
                val = _clean(val_el.get_text())
        if val:
            fields[label] = val
    return fields


def _parse_posted_date(box) -> str | None:
    """Extract exact posted timestamp from p[data-temp] on a search result box."""
    p_el = box.select_one("p[data-temp]")
    if p_el:
        dt = p_el.get("data-temp")
        if dt:
            return dt.strip()
        # Fallback: visible text "Posted on 2026-08-26 08:29:32"
        text = p_el.get_text(" ", strip=True)
        m = re.search(r"Posted on\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
        if m:
            return m.group(1)
    return None


def _parse_company_from_box(box) -> str | None:
    """Company via the SELECTORS['search.company'] fallback chain:
    1. p[data-temp] text — old markup 'CompanyName • Posted on …'
    2. img.jobpost-cat-box-logo alt — live markup
    3. any img alt
    A full miss bumps MISSING_FIELDS instead of silently returning None."""
    p_el = box.select_one(SELECTORS["search.company"][0])
    if p_el:
        text = p_el.get_text(" ", strip=True)
        if "•" in text:
            company = text.split("•")[0].strip()
            return company or None
        em = p_el.find("em")
        if em:
            before = text.replace(em.get_text(" ", strip=True), "").strip(" •")
            # live markup: the <em> is the whole "Posted on …" line — not a company
            if before and "posted on" not in before.lower():
                return before
    for sel in SELECTORS["search.company"][1:]:
        img = box.select_one(sel)
        if img and img.get("alt"):
            return _clean(img["alt"])
    MISSING_FIELDS["search.company"] += 1
    log.debug("search.company: no company found in job box (logo img missing)")
    return None


def _parse_desc_preview(box) -> tuple[str | None, str | None, str | None]:
    """Parse Location/Hours/Compensation from the description preview.
    Returns (location, hours, compensation).
    """
    desc_a = _first(box, "search.desc")
    if not desc_a:
        return None, None, None
    text = desc_a.get_text("\n", strip=True)
    location = hours = compensation = None
    for line in text.split("\n"):
        line = line.strip()
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
        elif line.lower().startswith("hours:"):
            hours = line.split(":", 1)[1].strip()
        elif line.lower().startswith("compensation:"):
            compensation = line.split(":", 1)[1].strip()
    return location, hours, compensation


def _parse_title_with_badge(h4) -> tuple[str, str | None]:
    """Parse an <h4> that may contain a <span class='badge'> work type.
    Returns (clean_title, work_type).
    """
    badge = h4.select_one(".badge")
    work_type = _clean(badge.get_text()) if badge else None
    # Get all text and subtract the badge text
    full_text = _clean(h4.get_text()) or ""
    if work_type and work_type in full_text:
        title = full_text.replace(work_type, "").strip()
    else:
        title = full_text
    return _clean(title) or "", work_type


# ── Public parsers ──────────────────────────────────────────────────────────

def parse_search_results(html: str) -> list[JobStub]:
    """Parse a search results page. Returns a list of JobStub (one per job box)."""
    soup = BeautifulSoup(html, "html.parser")
    sel = SELECTORS["search.job_box"][0]
    boxes = soup.select(sel)
    if not boxes:
        log.warning("No job boxes found (%s) — page structure may have changed", sel)
        return []

    stubs: list[JobStub] = []
    for box in boxes:
        # URL
        link_a = _first(box, "search.job_link")
        if not link_a:
            MISSING_FIELDS["search.job_link"] += 1
            continue
        raw_href = link_a.get("href", "")
        if raw_href.startswith("/"):
            raw_href = "https://www.onlinejobs.ph" + raw_href
        elif raw_href.startswith("//"):
            raw_href = "https:" + raw_href
        job_url = raw_href
        job_id = extract_job_id(job_url)

        # Title + work type
        title, work_type = None, None
        h4 = _first(box, "search.title")
        if h4:
            title, work_type = _parse_title_with_badge(h4)
        else:
            MISSING_FIELDS["search.title"] += 1

        # Company, posted date
        company = _parse_company_from_box(box)
        posted_date = _parse_posted_date(box)
        if not posted_date:
            MISSING_FIELDS["search.posted"] += 1

        # Salary
        salary_el = _first(box, "search.salary")
        salary = _clean(salary_el.get_text()) if salary_el else None
        if not salary:
            MISSING_FIELDS["search.salary"] += 1

        # Description preview → location, hours
        location, hours, _comp = _parse_desc_preview(box)
        if not location:
            MISSING_FIELDS["search.location"] += 1
        if not hours:
            MISSING_FIELDS["search.hours"] += 1

        # Skills
        skills = []
        for a in box.select(SELECTORS["search.skills"][0]):
            t = _clean(a.get_text())
            if t and t not in skills:
                skills.append(t)

        stubs.append(JobStub(
            job_id=job_id,
            job_url=job_url,
            title=title,
            work_type=work_type,
            company=company,
            posted_date=posted_date,
            salary=salary,
            location=location,
            hours=hours,
            skills=skills,
        ))

    log.info("Parsed %d job stubs from search results", len(stubs))
    return stubs


def parse_job_detail(html: str, url: str = "") -> JobDetail:
    """Parse a job detail page. Returns a JobDetail."""
    soup = BeautifulSoup(html, "html.parser")

    job_id = None
    title = None
    company = None
    description = None
    employer_id = None
    is_closed = False
    close_reason = None
    skills: list[str] = []
    fields: dict[str, str] = {}

    # Title + job ID
    h1 = _first(soup, "detail.title")
    if h1:
        title = _clean(h1.get_text())
        oid = h1.get("data-jobid")
        if oid:
            job_id = int(oid)
    else:
        # Fallback: extract from URL
        job_id = extract_job_id(url)
        if job_id is not None:
            MISSING_FIELDS["detail.title"] += 1

    # Company
    logo_h3 = _first(soup, "detail.company")
    if logo_h3:
        # Get employer ID from logo URL BEFORE extracting img
        logo_img = soup.select_one(SELECTORS["detail.employer"][0]) or logo_h3.find("img")
        if logo_img:
            src = logo_img.get("src", "")
            m = re.search(r"employer_logos/(\d+)/", src)
            if m:
                employer_id = int(m.group(1))
            logo_img.extract()
        company = _clean(logo_h3.get_text())
    else:
        MISSING_FIELDS["detail.company"] += 1

    # Description: always in <p id="job-description" class="job-description">
    desc_p = _first(soup, "detail.desc")
    if desc_p:
        oid = desc_p.get("data-jobid")
        if oid and not job_id:
            job_id = int(oid)
        description = desc_p.get_text("\n", strip=True)
        # Clean: remove first line if it repeats the title
        if description and title:
            lines = description.split("\n")
            if lines and title[:30] in lines[0]:
                lines = lines[1:]
            description = "\n".join(lines).strip() or None
    else:
        if not soup.select_one("h3.fs-12"):  # no structured card either → really gone
            MISSING_FIELDS["detail.desc"] += 1

    # Structured fields
    fields = _parse_structured_fields(soup)
    work_type = fields.get("TYPE OF WORK")
    salary = fields.get("WAGE / SALARY")
    hours_per_week = fields.get("HOURS PER WEEK")
    date_updated = fields.get("DATE UPDATED")

    # Skills
    skills = []
    for a in soup.select(SELECTORS["detail.skills"][0]):
        t = _clean(a.get_text())
        if t and t not in skills:
            skills.append(t)

    # Closed detection
    warning = _first(soup, "detail.closed")
    if warning and "closed" in warning.get_text().lower():
        is_closed = True
        close_reason = _clean(warning.get_text())

    if not is_closed:
        page_text = soup.get_text(" ", strip=True).lower()
        for phrase in (
            "this job has been closed",
            "job has been closed",
            "this job is no longer available",
            "position has been filled",
            # HTTP 410 page: <h1>Job No Longer Posted</h1> — "This job post has been
            # deleted and is no longer visible."
            "job no longer posted",
            "no longer visible",
            "has been deleted",
            # soft-404 page for a removed job (HTTP 404):
            "oops, we lost you there",
        ):
            if phrase in page_text:
                is_closed = True
                close_reason = phrase
                break

    return JobDetail(
        job_id=job_id,
        job_url=url,
        title=title,
        company=company,
        description=description,
        work_type=work_type,
        salary=salary,
        hours_per_week=hours_per_week,
        date_updated=date_updated,
        skills=skills,
        employer_id=employer_id,
        is_closed=is_closed,
        close_reason=close_reason,
    )


def get_total_results(html: str) -> int | None:
    """Extract total result count from the dataLayer in the page source."""
    m = re.search(r'search_result_count":(\d+)', html)
    return int(m.group(1)) if m else None
