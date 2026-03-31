"""
scraper.py — All scraping logic as importable, generator-based functions.
Each public function yields log-line strings so callers (CLI or web) can
stream progress in real time.

Functions:
    scrape_tags()               → yields log lines, returns tag list via final yield
    harvest_links(...)          → yields log lines
    check_and_fill_jobs(...)    → yields log lines
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
from urllib.parse import quote_plus, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL      = "https://onlinejobs.ph"
ALT_BASE_URL  = "https://www.onlinejobs.ph"
SEARCH_BASE   = f"{BASE_URL}/jobseekers/jobsearch"
ALT_SEARCH_BASE = f"{ALT_BASE_URL}/jobseekers/jobsearch"
JOBS_PER_PAGE = 30
REQUEST_DELAY = 0.5  # Delay between requests to avoid rate limiting

CLOSED_STATUS = "Closed"
OPEN_STATUS   = "Open"

CLOSED_PHRASES = [
    "this job has been closed",
    "job has been closed",
    "this job is no longer available",
    "position has been filled",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Updated dynamically when we discover which domain resolves in the current environment.
ACTIVE_BASE_URL = BASE_URL

# Global flag to signal scraping should stop
STOP_SCRAPING = False

def stop_scraping():
    """Signal all scraping operations to stop."""
    global STOP_SCRAPING
    STOP_SCRAPING = True

def reset_stop_flag():
    """Reset the stop flag before starting new scrape."""
    global STOP_SCRAPING
    STOP_SCRAPING = False

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonical_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = BASE_URL + raw
    elif not raw.startswith("http"):
        raw = BASE_URL + "/" + raw
    try:
        p = urlparse(raw.lower())
        netloc = p.netloc.removeprefix("www.")
        return urlunparse((p.scheme, netloc, p.path.rstrip("/"), "", "", ""))
    except Exception:
        return raw.lower().rstrip("/")


def search_url(keyword: str, page: int) -> str:
    offset = (page - 1) * JOBS_PER_PAGE
    search_base = f"{ACTIVE_BASE_URL}/jobseekers/jobsearch"
    path = search_base if offset == 0 else f"{search_base}/{offset}"
    kw = quote_plus((keyword or "").strip())
    return f"{path}?jobkeyword={kw}&skill_tags=&gig=on&partTime=on&fullTime=on&isFromJobsearchForm=1"


def fetch_page(url: str) -> BeautifulSoup:
    global ACTIVE_BASE_URL
    candidates = [url]
    if url.startswith(BASE_URL):
        candidates.append(url.replace(BASE_URL, ALT_BASE_URL, 1))
    elif url.startswith(ALT_BASE_URL):
        candidates.append(url.replace(ALT_BASE_URL, BASE_URL, 1))

    last_exc = None
    for candidate in candidates:
        try:
            resp = requests.get(candidate, headers=HEADERS, timeout=20)
            # Check for rate limit
            if resp.status_code == 429:
                raise requests.RequestException(f"HTTP 429 Rate Limit", response=resp)
            resp.raise_for_status()
            parsed = urlparse(resp.url)
            ACTIVE_BASE_URL = f"{parsed.scheme}://{parsed.netloc}".removesuffix("/")
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            last_exc = exc

    raise last_exc

# ── Tag scraping ──────────────────────────────────────────────────────────────

def scrape_tags() -> tuple[list[dict], list[str]]:
    """
    Fetch all skill tags from the search page.
    Returns (tags, log_lines) where tags is a list of {"id", "name"} dicts.
    """
    logs = []
    global ACTIVE_BASE_URL
    logs.append(f"🌐 Fetching tag catalogue from {SEARCH_BASE} …")

    resp = None
    last_exc = None
    for candidate in (SEARCH_BASE, ALT_SEARCH_BASE):
        try:
            resp = requests.get(candidate, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            parsed = urlparse(resp.url)
            ACTIVE_BASE_URL = f"{parsed.scheme}://{parsed.netloc}".removesuffix("/")
            if candidate != SEARCH_BASE:
                logs.append(f"ℹ️ Primary domain failed; using fallback domain: {candidate}")
            break
        except requests.RequestException as e:
            last_exc = e
            logs.append(f"⚠️ Failed to fetch {candidate}: {e}")

    if resp is None:
        logs.append(f"❌ Failed to fetch page from all known domains: {last_exc}")
        return [], logs

    seen: set[str] = set()
    tags: list[dict] = []
    soup = BeautifulSoup(resp.text, "html.parser")

    def add_tag(value: str, raw_name: str):
        tag_id = (value or "").strip()
        if not tag_id or not re.match(r"^\d+$", tag_id) or tag_id in seen:
            return

        # data-name is often HTML-encoded, e.g.
        # "&lt;small&gt;Category &lt;span&gt;»&lt;/span&gt; &lt;/small&gt; Skill"
        decoded = unescape(raw_name or "")
        name = clean(BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True))
        if not name:
            return

        seen.add(tag_id)
        tags.append({"id": tag_id, "name": name})

    # Strategy 1 (legacy): <select name="skill_tags"><option value="123">…</option></select>
    select_el = (
        soup.find("select", {"name": re.compile(r"skill_tags", re.I)})
        or soup.find("select", {"id": re.compile(r"skill_tags", re.I)})
    )
    if not select_el:
        for sel in soup.find_all("select"):
            options = sel.find_all("option")
            numeric = [o for o in options if re.match(r"^\d+$", (o.get("value") or "").strip())]
            if len(numeric) > 20:
                select_el = sel
                break
    if select_el:
        for option in select_el.find_all("option"):
            add_tag(option.get("value", ""), option.get_text())
        logs.append(f"ℹ️ Parsed {len(tags)} tag(s) from <select> options.")

    # Strategy 2 (current UI): dropdown links with data-id/data-name
    if not tags:
        for el in soup.select(".dropdown-skill-item[data-id][data-name], a[data-type='add'][data-id][data-name]"):
            add_tag(el.get("data-id", ""), el.get("data-name", "") or el.get_text())
        if tags:
            logs.append(f"ℹ️ Parsed {len(tags)} tag(s) from dropdown data-* attributes.")

    # Strategy 3 (fallback): broad scan for numeric data-id + data-name
    if not tags:
        for el in soup.select("[data-id][data-name]"):
            add_tag(el.get("data-id", ""), el.get("data-name", ""))
        if tags:
            logs.append(f"ℹ️ Parsed {len(tags)} tag(s) from generic data-* attributes.")

    if not tags:
        logs.append("❌ Could not locate skill tags in page HTML (select/options or dropdown data-*). Site layout may have changed.")
        return [], logs

    tags.sort(key=lambda t: t["name"].lower())
    logs.append(f"✅ Found {len(tags)} skill tag(s).")
    return tags, logs

# ── Link harvesting ───────────────────────────────────────────────────────────

def harvest_links(
    keyword: str,
    existing_links: set[str],
    hidden_links: set[str],
    posted_since: date | None = None,
):
    """
    Generator. Yields log-line strings.
    Last yielded value is a list of new job stub dicts when prefixed with "RESULT:".
    Actually yields plain strings; caller should check for the RESULT sentinel.

    Yields strings of the form:
        "LOG: <message>"       — display to user
        "RESULT: <json>"       — final list of new stubs (JSON-encoded)
    """
    import json

    collected: dict[str, dict] = {}
    today = date.today().isoformat()

    keyword = (keyword or "").strip()
    if not keyword:
        yield "LOG: ⚠ Empty keyword; nothing to search."
        yield "RESULT:[]"
        return

    yield f"LOG: 🔎 Searching keyword: {keyword}"
    if posted_since:
        yield f"LOG: 📅 Scraping until posts older than: {posted_since.isoformat()}"

    page = 1
    while True:
        # Check stop flag at start of each page
        if STOP_SCRAPING:
            yield "LOG: ⛔ Scraping stopped by user."
            break
            
        url = search_url(keyword, page)
        yield f"LOG:   Page {page} → {url}"

        try:
            soup = fetch_page(url)
        except requests.RequestException as exc:
            # Check if it's a 429 error
            if hasattr(exc, 'response') and exc.response is not None and exc.response.status_code == 429:
                yield "LOG:   ⛔ Rate limit (HTTP 429) detected — stopping."
            else:
                yield f"LOG:   ⚠  Could not fetch: {exc}"
            break

        boxes = soup.select(".jobpost-cat-box.latest-job-post")
        if not boxes:
            yield "LOG:   No job boxes found — stopping search."
            break

        found = skipped_existing = skipped_hidden = skipped_old = 0
        parsed_dates: list[date] = []
        for box in boxes:
            posted = _extract_posted_date(box)
            if posted:
                parsed_dates.append(posted)
            if posted_since and posted and posted < posted_since:
                skipped_old += 1
                continue

            link_tag = box.select_one("a[href^='/jobseekers/job/']")
            if not link_tag:
                continue
            link = canonical_url(link_tag.get("href", ""))
            if not link:
                continue
            if link in hidden_links:
                skipped_hidden += 1
                continue
            if link in existing_links:
                skipped_existing += 1
                continue
            collected[link] = {
                "job_link":   link,
                "search_tag": keyword,
                "date_found": today,
            }
            found += 1

        parts = [f"{found} new"]
        if skipped_existing:
            parts.append(f"{skipped_existing} already in DB")
        if skipped_hidden:
            parts.append(f"{skipped_hidden} hidden/suppressed")
        if skipped_old:
            parts.append(f"{skipped_old} older than cutoff")
        yield f"LOG:   → {', '.join(parts)}"

        if posted_since and parsed_dates and min(parsed_dates) < posted_since:
            yield "LOG:   Reached jobs older than cutoff date — stopping search."
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    new_stubs = list(collected.values())
    yield f"LOG: ✅ Harvest complete — {len(new_stubs)} new link(s) to check."
    yield f"RESULT:{json.dumps(new_stubs)}"


def _extract_posted_date(box) -> date | None:
    # Current search result cards expose timestamps via `data-temp`
    # and visible text like: "Posted on 2026-03-31 16:54:20".
    posted_el = box.select_one("p[data-temp], p[data-temp-2], em")
    if posted_el:
        for candidate in (
            posted_el.get("data-temp"),
            posted_el.get("data-temp-2"),
            clean(posted_el.get_text(" ", strip=True)),
        ):
            parsed = _parse_posted_date_string(candidate)
            if parsed:
                return parsed

    text = clean(box.get_text(" ", strip=True))
    m = re.search(r"posted on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", text, flags=re.I)
    if m:
        parsed = _parse_posted_date_string(m.group(1))
        if parsed:
            return parsed
    m = re.search(r"posted on\s+(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.I)
    if m:
        parsed = _parse_posted_date_string(m.group(1))
        if parsed:
            return parsed
    m = re.search(r"posted on\s+(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)", text, flags=re.I)
    if m:
        parsed = _parse_posted_date_string(m.group(1))
        if parsed:
            return parsed
    return None


def _parse_posted_date_string(raw: str | None) -> date | None:
    if not raw:
        return None

    raw = clean(raw)
    raw = re.sub(r"^posted on\s+", "", raw, flags=re.I).strip()

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None

# ── Job detail checking ───────────────────────────────────────────────────────

def check_and_fill_jobs(
    jobs: list[tuple[int, str, str]],   # (id, job_link, current_status)
    workers: int = 5,
):
    """
    Generator. Yields log-line strings as each job is checked.
    Last line is a SUMMARY: sentinel with counts.
    """
    if not jobs:
        yield "LOG: ✅ No jobs to check."
        return

    yield f"LOG: 🔍 Checking {len(jobs)} job(s) with {workers} worker(s)…"

    closed_count = open_count = 0
    detail_counts = {k: 0 for k in ("job_title", "company", "salary", "tags_found", "description")}
    results: list[tuple[int, dict]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_check_one, job): job for job in jobs}
        done = 0
        for future in as_completed(futures):
            done += 1
            job_id, details = future.result()
            results.append((job_id, details))

            status = details["status"]
            reason = details["reason"]
            url    = details["url"]

            if status == CLOSED_STATUS:
                closed_count += 1
                icon = "🔴"
            else:
                open_count += 1
                icon = "🟢"

            filled = [f for f in ("job_title", "company", "salary", "tags_found", "description") if details.get(f)]
            for f in filled:
                detail_counts[f] += 1

            short_url   = url.replace("https://www.onlinejobs.ph", "")
            detail_note = f"  ✅ {', '.join(filled)}" if filled else "  ⚠  no details"
            yield f"LOG: [{done}/{len(jobs)}] {icon} {status:<8} {short_url} ({reason}){detail_note}"

    import json
    summary = {
        "open": open_count,
        "closed": closed_count,
        "total": len(jobs),
        "details": detail_counts,
        "results": [(jid, det) for jid, det in results],
    }
    yield f"SUMMARY:{json.dumps(summary, default=str)}"


def _check_one(job: tuple[int, str, str]) -> tuple[int, dict]:
    job_id, url, _ = job
    time.sleep(REQUEST_DELAY)
    details = _scrape_job_page(url)
    details["url"] = url
    return job_id, details


def _scrape_job_page(url: str) -> dict:
    result: dict = {
        "status":      CLOSED_STATUS,
        "reason":      "",
        "description": None,
        "job_title":   None,
        "company":     None,
        "salary":      None,
        "tags_found":  None,
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        # Check for rate limit
        if resp.status_code == 429:
            result["reason"] = "HTTP 429 Rate Limit"
            return result
        if resp.status_code == 404:
            result["reason"] = "404 Not Found"
            return result
        if resp.status_code >= 400:
            result["reason"] = f"HTTP {resp.status_code}"
            return result

        soup      = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text(separator=" ").lower()

        closed_banner = soup.select_one("h3.text-warning")
        if closed_banner and "closed" in closed_banner.get_text().lower():
            result["status"] = CLOSED_STATUS
            result["reason"] = "Job closed (Banner)"
            _fill_details(soup, result)
            return result

        for phrase in CLOSED_PHRASES:
            if phrase in page_text:
                result["status"] = CLOSED_STATUS
                result["reason"] = "Job closed (Text)"
                _fill_details(soup, result)
                return result

        result["status"] = OPEN_STATUS
        result["reason"] = "Open"
        _fill_details(soup, result)
        return result

    except requests.exceptions.Timeout:
        result["reason"] = "Timeout"
    except requests.exceptions.ConnectionError:
        result["reason"] = "Connection error"
    except Exception as exc:
        result["reason"] = f"Error: {exc}"
    return result


def _fill_details(soup: BeautifulSoup, result: dict) -> None:
    result["description"] = _scrape_description(soup)
    result["job_title"]   = _scrape_title(soup)
    result["company"]     = _scrape_company(soup)
    result["salary"]      = _scrape_salary(soup)
    result["tags_found"]  = _scrape_tags_found(soup)


def _scrape_description(soup: BeautifulSoup) -> str | None:
    el = soup.select_one("#job-description")
    if el:
        return clean(el.get_text(separator="\n"))
    overview = soup.find("div", class_="card-header", string=lambda t: t and "JOB OVERVIEW" in t)
    if overview:
        body = overview.find_next_sibling("div", class_="card-body")
        if body:
            return clean(body.get_text(separator="\n"))
    return None


def _scrape_title(soup: BeautifulSoup) -> str | None:
    for sel in ("h1.job-title", "h2.job-title", ".job-header h1", ".job-header h2", "h1", "h2"):
        el = soup.select_one(sel)
        if el:
            text = clean(el.get_text())
            if text:
                return text
    title_tag = soup.find("title")
    if title_tag:
        raw = clean(title_tag.get_text())
        return raw.split("|")[0].strip() or None
    return None


def _scrape_company(soup: BeautifulSoup) -> str | None:
    for sel in (".employer-name", ".company-name", "a[href*='/jobseekers/employer/']", ".job-company"):
        el = soup.select_one(sel)
        if el:
            text = clean(el.get_text())
            if text:
                return text
    return None


def _scrape_salary(soup: BeautifulSoup) -> str | None:
    for sel in (".salary", ".pay-rate", "dd.col", "[data-salary]"):
        el = soup.select_one(sel)
        if el:
            text = clean(el.get_text())
            if text:
                return text
    return None


def _scrape_tags_found(soup: BeautifulSoup) -> str | None:
    tag_texts: list[str] = []

    for a in soup.select("a[href*='skill_tags='], a[href*='/jobseekers/jobsearch'][href*='skill']"):
        text = clean(a.get_text())
        if text and text not in tag_texts:
            tag_texts.append(text)
    if tag_texts:
        return ", ".join(tag_texts)

    for sel in (
        ".job-tags a", ".job-tags span",
        ".skill-tags a", ".skill-tags span",
        ".tags a", ".tags span",
        "[class*='tag'] a", "[class*='badge'] a",
    ):
        for el in soup.select(sel):
            text = clean(el.get_text())
            if text and text not in tag_texts:
                tag_texts.append(text)
        if tag_texts:
            return ", ".join(tag_texts)

    for a in soup.select("a[href*='jobsearch']"):
        href = a.get("href", "")
        if "skill_tags" in href or "tag" in href.lower():
            text = clean(a.get_text())
            if text and text not in tag_texts:
                tag_texts.append(text)

    return ", ".join(tag_texts) if tag_texts else None
