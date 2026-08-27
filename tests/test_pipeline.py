"""tests/test_pipeline.py — enrich() generator events (fake client, no network).

Covers the SSE payload contract the frontend depends on (progress counter,
error branch) and the crash-regression: closing the generator while workers
are in flight must never raise (previously: "RuntimeError: cannot join
current thread" when CPython's gc reclaimed the generator frame on a pool
worker thread — see server_e2e.log).
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.pipeline import enrich, PipelineEvent
from tests.test_parsers import CLOSED_HTML, DETAIL_HTML


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
