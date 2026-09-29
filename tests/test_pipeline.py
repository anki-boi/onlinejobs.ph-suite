"""tests/test_pipeline.py — enrich() generator events (fake client, no network).

Covers the SSE payload contract the frontend depends on (progress counter,
error branch) and the crash-regression: closing the generator while workers
are in flight must never raise (previously: "RuntimeError: cannot join
current thread" when CPython's gc reclaimed the generator frame on a pool
worker thread — see server_e2e.log).
"""

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.pipeline import enrich, harvest
from tests.test_parsers import CLOSED_HTML, DETAIL_HTML, SEARCH_HTML


class FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeClient:
    stopped = False

    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        v = self.pages[url]
        return v if isinstance(v, FakeResp) else FakeResp(v)


class BoomClient:
    stopped = False

    def get(self, url):
        raise Exception("boom")


class SlowClient:
    """Staggered per-job durations: job N sleeps (0.2 + 0.4*N) s, so the first
    completion leaves the other workers deterministically in flight."""
    stopped = False

    def get(self, url):
        n = int(url.rsplit("/", 1)[-1])
        time.sleep(0.2 + 0.4 * n)
        return FakeResp(DETAIL_HTML)


def test_enrich_banner_progress_and_summary():
    client = FakeClient({
        "http://a/job/1": DETAIL_HTML,
        "http://a/job/2": CLOSED_HTML,
    })
    events = list(enrich(client, [(11, "http://a/job/1"), (12, "http://a/job/2")], workers=2))

    assert events[0].type == "log"
    assert events[0].message.startswith("Enriching 2 job(s)")

    results = [e for e in events if e.type == "enrich_result"]
    assert len(results) == 2
    assert sorted(e.data["progress"] for e in results) == ["1/2", "2/2"]
    # success payload carries the fields the frontend shows
    ok = next(e for e in results if not e.data.get("is_closed"))
    assert ok.data["title"]
    assert ok.data["row_id"] in (11, 12)
    closed = next(e for e in results if e.data.get("is_closed"))
    assert closed.data["close_reason"]

    assert events[-1].type == "summary"
    assert events[-1].data["total"] == 2
    assert events[-1].data["open"] == 1
    assert events[-1].data["closed"] == 1


def test_enrich_error_event_has_progress():
    events = list(enrich(BoomClient(), [(1, "http://a/x")]))
    errs = [e for e in events if e.type == "enrich_result" and e.data.get("error")]
    assert len(errs) == 1
    assert errs[0].data["progress"] == "1/1"
    assert errs[0].data["error"] == "boom"
    summary = events[-1]
    assert summary.type == "summary"
    assert summary.data["errors"] == 1


def test_enrich_empty():
    events = list(enrich(FakeClient({}), []))
    assert len(events) == 1
    assert events[0].type == "log"
    assert events[0].message == "No jobs to enrich"


def test_enrich_stop():
    client = FakeClient({f"http://a/job/{i}": DETAIL_HTML for i in range(5)})

    def get_then_stop(url):
        resp = FakeResp(DETAIL_HTML)
        client.stopped = True  # first request observed → user pressed Stop
        return resp

    client.get = get_then_stop
    events = list(enrich(client, [(i, f"http://a/job/{i}") for i in range(5)], workers=2))
    texts = [e.message for e in events]
    assert any("Stopped during enrich" in t for t in texts)
    assert events[-1].type == "summary"


def test_close_generator_while_workers_inflight():
    """B2 regression: gen.close() (client disconnect / GC) must not raise,
    from whichever thread it happens on, and must drain the pool."""
    client = SlowClient()
    gen = enrich(client, [(i, f"http://a/job/{i}") for i in range(3)], workers=3)

    banner = next(gen)  # "Enriching 3 job(s)…" — generator suspended before the pool exists
    first = next(gen)   # resumes → pool spawns; returns when the first future completes (0.2 s)
    assert banner.type == "log"
    assert first.type == "enrich_result"
    # The other two workers (0.6 s / 1.0 s) are still in flight now.
    before = {t.name for t in threading.enumerate() if t.name.startswith("enrich")}
    assert before, "workers should be in flight when we close"

    gen.close()  # must not raise, regardless of which thread gc picked

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(t.name for t in threading.enumerate() if t.name.startswith("enrich")):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("enrich worker threads did not drain after close()")


def test_exhaustive_drain_after_full_iteration():
    """Normal completion: all pool threads join cleanly."""
    client = SlowClient()
    events = list(enrich(client, [(i, f"http://a/job/{i}") for i in range(2)], workers=2))
    assert events[-1].type == "summary"
    time.sleep(0.1)
    assert not any(t.name for t in threading.enumerate() if t.name.startswith("enrich"))


def test_enrich_http_404_and_410_mark_closed():
    """404 (removed) and 410 ("Job No Longer Posted") both → Closed, no parse attempt."""
    client = FakeClient({
        "http://a/job/6": FakeResp("", status_code=404),
        "http://a/job/7": FakeResp("<html><body><h1>Job No Longer Posted</h1></body></html>", status_code=410),
    })
    events = list(enrich(client, [(6, "http://a/job/6"), (7, "http://a/job/7")], workers=2))
    results = {e.data["row_id"]: e.data for e in events if e.type == "enrich_result"}
    assert results[6]["is_closed"] is True and "404" in results[6]["close_reason"]
    assert results[7]["is_closed"] is True and "410" in results[7]["close_reason"]
    assert events[-1].type == "summary"
    assert events[-1].data["closed"] == 2
    assert events[-1].data["open"] == 0
    assert events[-1].data["errors"] == 0


def test_enrich_gone_page_parse_also_closed():
    """If a 410-style page ever arrives with status 200, phrase detection still catches it."""
    from tests.test_parsers import GONE_HTML
    client = FakeClient({"http://a/job/8": GONE_HTML})
    events = list(enrich(client, [(8, "http://a/job/8")], workers=1))
    results = [e for e in events if e.type == "enrich_result"]
    assert results[0].data["is_closed"] is True
    assert events[-1].data["closed"] == 1


# ── Parse watchdog: a site redesign must fail loudly, not silently ─────────

class AnyPageClient:
    """Serves the same html for every URL (harvest builds URLs from base_url)."""
    stopped = False
    base_url = "http://x"

    def __init__(self, html):
        self.html = html

    def get(self, url):
        # The real site runs out of results pages; mimic that from page 2 on,
        # otherwise harvest() walks pages forever.
        if "/jobsearch/" in url or "/c/" in url:
            return FakeResp(self.html.replace("jobpost-cat-box", ""))
        return FakeResp(self.html)


def _with_data_layer(html, count):
    return html.replace("<html><body>",
                        f'<html><body><script>window.dataLayer = [{{"search_result_count":{count}}}]</script>')


def test_harvest_watchdog_zero_stubs_with_results_claimed():
    """Site claims N results but 0 job boxes parse → structure change, warn loudly."""
    html = _with_data_layer(SEARCH_HTML, 42).replace("jobpost-cat-box", "jobpost-renamed-box")
    events = list(harvest(AnyPageClient(html)))
    errs = [e for e in events if e.type == "error"]
    assert any("job boxes parsed" in e.message for e in errs)


def test_harvest_no_watchdog_when_parsing_ok():
    events = list(harvest(AnyPageClient(SEARCH_HTML)))
    assert not any(e.type == "error" for e in events)
    stubs = sum(len(e.data["stubs"]) for e in events if e.type == "harvest_result")
    assert stubs == 2


def test_enrich_watchdog_zero_titles():
    """All fetched 200 detail pages parse no title → structure change, warn."""
    client = FakeClient({
        "http://a/job/1": "<html><body><div>no h1 here</div></body></html>",
        "http://a/job/2": "<html><body><div>still none</div></body></html>",
    })
    events = list(enrich(client, [(1, "http://a/job/1"), (2, "http://a/job/2")], workers=2))
    errs = [e for e in events if e.type == "error"]
    assert any("none parsed a title" in e.message for e in errs)


def test_enrich_watchdog_ignores_closed_and_ok_pages():
    """Closed pages (gone phrase) don't count as parse misses; a parsed title silences the watch."""
    from tests.test_parsers import GONE_HTML
    client = FakeClient({
        "http://a/job/1": GONE_HTML,
        "http://a/job/2": "<html><body><div>no h1</div></body></html>",
        "http://a/job/3": DETAIL_HTML,
    })
    events = list(enrich(client, [(1, "http://a/job/1"), (2, "http://a/job/2"), (3, "http://a/job/3")], workers=3))
    assert not any(e.type == "error" for e in events)


def test_harvest_multiple_skills_is_or_not_and():
    """Multiple selected skills = one search per skill (OR). OJ.ph ANDs a
    comma-separated skill_tags value, so a single multi-skill query returns
    nothing (the reported bug)."""
    box = ('<div class="jobpost-cat-box latest-job-post">'
           '<h4>{t} <span class="badge">Any</span></h4>'
           '<p data-temp="2026-08-25 14:30:00"><em>Co</em></p>'
           '<a class="joblink" href="/jobseekers/job/{s}-{i}">Apply</a>'
           '</div>')
    pages = {
        1: "<html><body><script>window.dataLayer = [{\"search_result_count\":1}]</script>"
           f'<div class="job-list">{box.format(t="Digital Marketing Director", s="digital-marketing", i=901)}</div></body></html>',
        2: "<html><body><script>window.dataLayer = [{\"search_result_count\":1}]</script>"
           f'<div class="job-list">{box.format(t="Python Developer (Jr)", s="python-developer", i=902)}</div></body></html>',
    }
    import re
    class Rec:
        stopped = False
        base_url = "http://x"
        urls = []
        def get(self, url):
            self.urls.append(url)
            m = re.search(r"skill_tags=(\d+)", url)
            return FakeResp(pages[int(m.group(1))] if m else pages[1])
    client = Rec()
    events = list(harvest(client, skill_ids=[1, 2]))
    skill_urls = [u for u in client.urls if "skill_tags=" in u]
    assert len(skill_urls) == 2, f"expected one search per skill, got: {skill_urls}"
    assert any("skill_tags=1" in u for u in skill_urls)
    assert any("skill_tags=2" in u for u in skill_urls)
    summary = [e for e in events if e.type == "summary"][0]
    assert summary.data["new"] == 2


def _kw_pages(kw_to_job):
    """Fake search pages keyed by the jobkeyword the site was asked for."""
    box = ('<div class="jobpost-cat-box latest-job-post">'
           '<h4>{t} <span class="badge">Any</span></h4>'
           '<p data-temp="2026-08-25 14:30:00"><em>Co</em></p>'
           '<a class="joblink" href="/jobseekers/job/{s}-{i}">Apply</a>'
           '</div>')
    out = {}
    for kw, (i, title) in kw_to_job.items():
        out[kw] = ("<html><body><script>window.dataLayer = [{\"search_result_count\":1}]</script>"
                   + box.format(t=title, s=title.lower().replace(" ", "-"), i=i)
                   + "</body></html>")
    return out


class _KwClient:
    """Records every URL and answers by the jobkeyword in it."""
    stopped = False
    base_url = "http://x"

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        m = re.search(r"jobkeyword=([^&]*)", url)
        key = (m.group(1).replace("%20", " ").strip() if m else "")
        return FakeResp(self.pages.get(key) or self.pages.get("") or "<html><body></body></html>")


def test_each_keyword_gets_its_own_search():
    """B1: chips joined with ", " were sent as ONE jobkeyword, i.e. the literal
    phrase "bookkeeper, va" — which matches nothing on the site."""
    client = _KwClient(_kw_pages({"bookkeeper": (901, "Bookkeeper needed"),
                                  "va": (902, "VA for admin work")}))
    events = list(harvest(client, keyword="bookkeeper, va"))
    kws = [u.split("jobkeyword=")[1].split("&")[0] for u in client.urls if "jobkeyword=" in u]
    assert "bookkeeper" in kws and "va" in kws, f"keywords not searched separately: {kws}"
    assert not any("bookkeeper%2C%20va" in u or "bookkeeper, va" in u for u in client.urls)
    summary = [e for e in events if e.type == "summary"][0]
    assert summary.data["new"] == 2, "both keyword searches contributed jobs"


def test_keyword_no_longer_filters_results_by_title():
    """B2 (D1): a `bookkeeper` search used to drop every stub whose title didn't
    contain the word — killing "Part-time accountant" and the rest."""
    client = _KwClient({"bookkeeper": _kw_pages({"": (0, "")})[""]})
    # one page whose job title does NOT contain the keyword
    html = ('<html><body><script>window.dataLayer = [{"search_result_count":1}]</script>'
            '<div class="jobpost-cat-box latest-job-post">'
            '<h4>Part-time accountant <span class="badge">Any</span></h4>'
            '<p data-temp="2026-08-25 14:30:00"><em>Co</em></p>'
            '<a class="joblink" href="/jobseekers/job/part-time-accountant-903">Apply</a>'
            '</div></body></html>')
    client.pages = {"bookkeeper": html}
    events = list(harvest(client, keyword="bookkeeper"))
    stubs = [s for e in events if e.type == "harvest_result" for s in e.data["stubs"]]
    assert [s["title"] for s in stubs] == ["Part-time accountant"], \
        "the site already searched; the client-side title filter threw results away"


def test_pagination_stop_is_per_search_not_global():
    """B8: total_seen accumulated across every search target and was compared to
    a per-query total, so a second search stopped after page 1."""
    box = ('<div class="jobpost-cat-box latest-job-post">'
           '<h4>{t} <span class="badge">Any</span></h4>'
           '<p data-temp="2026-08-25 14:30:00"><em>Co</em></p>'
           '<a class="joblink" href="/jobseekers/job/{s}-{i}">Apply</a>'
           '</div>')

    class TwoPageClient:
        stopped = False
        base_url = "http://x"

        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            page = url.rstrip("/").rsplit("/", 1)[-1]
            n = int(page) if page.isdigit() else 0
            kw = url.split("jobkeyword=")[1].split("&")[0].replace("%20", " ")
            if n > 1:
                return FakeResp("<html><body></body></html>")   # no more boxes
            title = f"{kw} job {n}"
            return FakeResp(
                "<html><body><script>window.dataLayer = [{\"search_result_count\":60}]</script>"
                + box.format(t=title, s=title.lower().replace(" ", "-"),
                             i=1000 + abs(hash(kw)) % 500 + n)
                + "</body></html>")

    client = TwoPageClient()
    events = list(harvest(client, keyword="alpha, beta"))
    assert sum(len(e.data["stubs"]) for e in events if e.type == "harvest_result") >= 2
    # each keyword must have been asked for page 2 — the old shared counter
    # stopped the second keyword after page 1 because the first had "seen" 60
    per_kw = {}
    for u in client.urls:
        kw = u.split("jobkeyword=")[1].split("&")[0].replace("%20", " ")
        per_kw.setdefault(kw, []).append(u.rstrip("/").rsplit("/", 1)[-1])
    for kw in ("alpha", "beta"):
        assert len(per_kw.get(kw, [])) >= 2, f"{kw} stopped too early: {per_kw.get(kw)}"


def test_category_slug_is_url_encoded():
    """B5: 'Hosting & Infrastructure Management' interpolated raw into the path
    segment put a literal & in the URL and hit a different page."""
    from scraper.pipeline import search_url
    u = search_url("http://x", "", 0, category="hosting-&-infrastructure-management")
    assert "&" not in u.split("/search/c/")[1].split("?")[0], u
    assert "hosting-%26-infrastructure-management" in u


def test_harvest_emits_the_derived_category():
    """L4: the stub carries the category parsed from its tag href."""
    html = ('<html><body><script>window.dataLayer = [{"search_result_count":1}]</script>'
            '<div class="jobpost-cat-box latest-job-post">'
            '<h4>Email Marketing VA <span class="badge">Any</span></h4>'
            '<p data-temp="2026-08-25 14:30:00"><em>Co</em></p>'
            '<a class="joblink" href="/jobseekers/job/email-marketing-va-904">Apply</a>'
            '<div class="job-tag"><a class="badge" href="/jobseekers/search/c/marketing--email-marketing">Email Marketing</a></div>'
            '</div></body></html>')
    events = list(harvest(_KwClient({"": html}), keyword=""))
    stubs = [s for e in events if e.type == "harvest_result" for s in e.data["stubs"]]
    assert stubs and stubs[0]["category"] == "marketing"


class _StormClient:
    """Every detail page comes back Cloudflare 521 until the retries run out."""
    stopped = False
    base_url = "http://x"

    def set_stop(self, token):
        self.stopped = token.stopped

    def get(self, url):
        from scraper.client import RateLimitExhausted
        raise RateLimitExhausted("Failed after 3 retries", status=521)


def test_enrich_stops_on_a_rate_limit_storm():
    """L6: a live run burned minutes grinding HTTP 521s one job at a time. Once
    most fetches are rate limits, enrich stops and leaves the rest queued."""
    jobs = [(i, f"http://a/job/{i}") for i in range(1, 41)]
    events = list(enrich(_StormClient(), jobs, workers=2))
    errs = [e for e in events if e.type == "error"]
    assert any("rate-limiting" in e.message for e in errs), [e.message for e in errs]
    results = [e for e in events if e.type == "enrich_result"]
    assert len(results) < len(jobs), "it stopped early instead of trying all 40"
    summary = [e for e in events if e.type == "summary"][0]
    assert summary.data["rate_limited"] >= 5


def test_one_rate_limited_job_does_not_stop_the_run():
    """One bad page is not a storm — the run continues."""
    class MostlyFine:
        stopped = False
        base_url = "http://x"
        def set_stop(self, token): self.stopped = token.stopped
        def get(self, url):
            from scraper.client import RateLimitExhausted
            if url.endswith("/job/1"):
                raise RateLimitExhausted("nope", status=521)
            return FakeResp(DETAIL_HTML)

    events = list(enrich(MostlyFine(), [(i, f"http://a/job/{i}") for i in range(1, 6)], workers=2))
    assert not any("rate-limiting" in e.message for e in events if e.type == "error")
    assert sum(1 for e in events if e.type == "enrich_result") == 5
