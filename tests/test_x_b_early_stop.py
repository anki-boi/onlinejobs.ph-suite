"""
X-B (F3): harvest stops paging at the recency window instead of walking the archive.

The board is newest-first (verified against both live search fixtures), so the oldest
card on a page is the tail of the list. Once it is past the window, every later page
is listings the window would drop anyway — and each of those pages is a Cloudflare
request paid for nothing.
"""

from datetime import date, timedelta

from scraper.pipeline import harvest, search_url

BASE = "https://www.onlinejobs.ph"
WINDOW = "2026-09-20"

FRESH = [(101, "2026-09-28 09:00:00"), (102, "2026-09-25 09:00:00")]
STALE_TAIL = [(103, "2026-09-24 09:00:00"), (104, "2026-09-12 09:00:00")]
UNDATED = [(105, None), (106, None)]


def page(pairs):
    boxes = "".join(
        f'<div class="jobpost-cat-box latest-job-post">'
        f'<a href="{BASE}/jobseekers/job/{jid}">'
        f'<h4 class="jobpost-cat-box-title">Job {jid} '
        f'<span class="badge">Full Time</span></h4></a>'
        + (f'<p data-temp="{d}">Posted on {d}</p>' if d else "<p>no date</p>")
        + "</div>"
        for jid, d in pairs)
    return f"<html><body>{boxes}</body></html>"


class PageClient:
    """Serves the prepared pages in order and records what was asked for."""
    stopped = False
    base_url = BASE

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return type("Resp", (), {"text": self.pages[url], "status_code": 200})()


def _client(pages):
    urls = [search_url(BASE, "", i) for i in range(len(pages))]
    client = PageClient(dict(zip(urls, [page(p) if p else "<html></html>" for p in pages])))
    return client, urls


def test_a_stale_tail_ends_the_search():
    client, urls = _client([FRESH, STALE_TAIL, FRESH, FRESH])
    events = list(harvest(client, posted_since=WINDOW))
    assert client.urls == urls[:2], "stopped after the page whose oldest card is stale"
    assert any("recency window ends here" in e.message for e in events), "it says why"


def test_without_a_window_it_keeps_paging():
    client, urls = _client([FRESH, STALE_TAIL, FRESH, None])
    list(harvest(client))
    assert client.urls == urls, "no window → no early stop"


def test_an_undated_page_is_not_a_reason_to_stop():
    """A card whose date cannot be read must not end the list."""
    client, urls = _client([UNDATED, FRESH, None])
    events = list(harvest(client, posted_since=WINDOW))
    assert client.urls == urls, "it paged past the undated page"
    assert not any("recency window" in e.message for e in events)


def test_auto_run_uses_the_configured_window(monkeypatch, tmp_db):
    import app.scheduler as sched
    seen = {}

    def fake_harvest(client, **kw):
        seen.update(kw)
        return iter([])

    monkeypatch.setattr(sched, "harvest", fake_harvest)
    sched.run_once(type("C", (), {"stopped": False})(), tmp_db, lambda *a: None,
                   {"harvest_max_age_days": 3})
    assert seen["posted_since"] == (date.today() - timedelta(days=3)).isoformat()

    seen.clear()
    sched.run_once(type("C", (), {"stopped": False})(), tmp_db, lambda *a: None,
                   {"harvest_max_age_days": 0})
    assert seen["posted_since"] is None, "0 pages the whole archive"
