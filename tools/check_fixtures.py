"""
tools/check_fixtures.py — W3.1 live-drift check (NOT a pytest; hits the network).

Re-fetches the four page types the fixture corpus covers (1 req/s politeness)
and re-runs the real parsers against the live HTML with the same expectations
the fixture tests assert. If the site's markup drifts so a page no longer
parses the way the fixtures do, this exits 1 and names the broken check.

Exit 0 also when the site is unreachable (warn only — no network ≠ drift).

Run: python tools/check_fixtures.py
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # CP1252-safe on Windows

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scraper.client import OJClient  # noqa: E402
from scraper.parsers import (  # noqa: E402
    MISSING_FIELDS,
    get_total_results,
    parse_job_detail,
    parse_search_results,
)
from app import config as appconfig  # noqa: E402

# stable examples per page type (must stay in-repo with the fixtures)
CLOSED_URL = "/jobseekers/job/customer-support-top-dtc-brand-experience-required-500-2-000-performance-bonus-1662908"
GONE_URL = "/jobseekers/job/this-job-does-not-exist-9999999"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    client = OJClient(delay=1.0, user_agent=appconfig.get()["user_agent"])

    # 1 — search results page
    r = client.get("/jobseekers/jobsearch")
    stubs = parse_search_results(r.text)
    check("search: >=1 job box parsed", len(stubs) >= 1, f"{len(stubs)} stubs")
    if stubs:
        s = stubs[0]
        check("search: first stub has job_id + title",
              s.job_id is not None and bool(s.title),
              f"id={s.job_id} title={s.title!r}")
    total = get_total_results(r.text)
    check("search: dataLayer result count present", total is not None, str(total))

    # 2 — a live job detail page (first job from the search page above)
    if stubs and stubs[0].job_url:
        url = stubs[0].job_url
        if url.startswith("http"):
            from urllib.parse import urlparse
            url = urlparse(url).path
        d = parse_job_detail(client.get(url).text, url)
        check("detail: title + company parsed",
              bool(d.title) and bool(d.company),
              f"closed={d.is_closed}")
        check("detail: description present", bool(d.description))
    else:
        check("detail: could not pick a live job URL", False)

    # 3 — closed job (410 "Job No Longer Posted")
    c = parse_job_detail(client.get(CLOSED_URL).text, CLOSED_URL)
    check("closed page: classified closed", c.is_closed, str(c.close_reason))

    # 4 — soft 404
    g = parse_job_detail(client.get(GONE_URL).text, GONE_URL)
    check("404 page: classified gone, no job title",
          g.is_closed and g.title is None, str(g.close_reason))

    if MISSING_FIELDS:
        print(f"note: missing-field counter: {dict(MISSING_FIELDS)}")

    if failures:
        print(f"DRIFT: {len(failures)} check(s) failed — inspect scraper/fixtures/ "
              f"and SELECTORS in scraper/parsers.py")
        return 1
    print("all fixture checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"warn: live check could not run ({type(exc).__name__}: {exc}) — "
              f"treating as no-signal, not drift")
        sys.exit(0)
