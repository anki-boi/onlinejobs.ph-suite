"""
scraper/parsers.py — Pure HTML→dataclass parsers. No I/O, no globals.

Each function takes an HTML string and returns structured data.
This is the testable core of the scraper.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


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
    for h3 in soup.select("h3.fs-12"):
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
    """Extract company name from p[data-temp] text (before the bullet)."""
    p_el = box.select_one("p[data-temp]")
    if not p_el:
        return None
    text = p_el.get_text(" ", strip=True)
    # Pattern: "CompanyName •    Posted on ..."
    if "•" in text:
        company = text.split("•")[0].strip()
        return company or None
    # Some boxes have no bullet — try the <em> tag
    em = p_el.find("em")
    if em:
        before = p_el.get_text(" ", strip=True).replace(
            em.get_text(" ", strip=True), ""
        ).strip(" •")
        return before or None
    return None


def _parse_desc_preview(box) -> tuple[str | None, str | None, str | None]:
    """Parse Location/Hours/Compensation from the description preview.
    Returns (location, hours, compensation).
    """
    desc_a = box.select_one(".desc a:not([target])")
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
    boxes = soup.select(".jobpost-cat-box.latest-job-post")
    if not boxes:
        log.warning("No job boxes found (.jobpost-cat-box.latest-job-post) — page structure may have changed")
        return []

    stubs: list[JobStub] = []
    for box in boxes:
        # URL
        link_a = box.select_one("a[href*='/jobseekers/job/']")
        if not link_a:
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
        h4 = box.select_one("h4")
        if h4:
            title, work_type = _parse_title_with_badge(h4)

        # Company, posted date
        company = _parse_company_from_box(box)
        posted_date = _parse_posted_date(box)

        # Salary
        salary_el = box.select_one("dd.col")
        salary = _clean(salary_el.get_text()) if salary_el else None

        # Description preview → location, hours
        location, hours, _comp = _parse_desc_preview(box)

        # Skills
        skills = []
        for a in box.select(".job-tag a"):
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
    h1 = soup.select_one("h1.job__title")
    if h1:
        title = _clean(h1.get_text())
        oid = h1.get("data-jobid")
        if oid:
            job_id = int(oid)
    else:
        # Fallback: extract from URL
        job_id = extract_job_id(url)

    # Company
    logo_h3 = soup.select_one("h3.job__logo")
    if logo_h3:
        # Get employer ID from logo URL BEFORE extracting img
        logo_img = logo_h3.find("img")
        if logo_img:
            src = logo_img.get("src", "")
            m = re.search(r"employer_logos/(\d+)/", src)
            if m:
                employer_id = int(m.group(1))
            logo_img.extract()
        company = _clean(logo_h3.get_text())

    # Description: always in <p id="job-description" class="job-description">
    desc_p = soup.select_one("p#job-description")
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

    # Structured fields
    fields = _parse_structured_fields(soup)
    work_type = fields.get("TYPE OF WORK")
    salary = fields.get("WAGE / SALARY")
    hours_per_week = fields.get("HOURS PER WEEK")
    date_updated = fields.get("DATE UPDATED")

    # Skills
    for a in soup.select("a.card-worker-topskill"):
        t = _clean(a.get_text())
        if t and t not in skills:
            skills.append(t)

    # Closed detection
    warning = soup.select_one("h3.text-warning, .alert-warning")
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
