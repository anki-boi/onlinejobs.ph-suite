"""tests/test_client.py -- OJClient retry-after parsing."""
import pytest
from scraper.client import OJClient


class TestParseRetryAfter:
    def test_seconds(self):
        assert OJClient._parse_retry_after("30") == 30.0

    def test_float(self):
        assert OJClient._parse_retry_after("5.5") == pytest.approx(5.5)

    def test_none(self):
        assert OJClient._parse_retry_after(None) is None

    def test_empty_string(self):
        assert OJClient._parse_retry_after("") is None

    def test_invalid_date(self):
        # garbage → falls through to None
        assert OJClient._parse_retry_after("not-a-date") is None


class TestThrottle:
    """Verify that _throttle enforces the configured delay."""
    def test_enforces_delay(self, monkeypatch):
        import time as _time
        calls = []
        monkeypatch.setattr(_time, "sleep", lambda s: calls.append(s))
        client = OJClient(delay=0.1)
        # First call — no prior request, so sleep(0) effectively (but we don't assert 0)
        client._throttle()
        # Second call immediately should wait at least delay seconds
        client._throttle()
        assert calls[-1] >= 0.09  # ~0.1s minus tiny float tolerance


class TestCookies:
    """L2 (audit): `oj_cookies` was documented in the README, redacted in
    /api/config and covered by a redaction test — and never actually sent.
    Scraping logged-out is why employer_id is NULL on 90% of rows."""

    def test_cookies_are_sent(self):
        c = OJClient(cookies="ci_session=abc; remember_token=xyz")
        assert c.session.headers["Cookie"] == "ci_session=abc; remember_token=xyz"

    def test_no_cookies_no_header(self):
        c = OJClient()
        assert "Cookie" not in c.session.headers

    def test_blank_cookies_are_ignored(self):
        c = OJClient(cookies="   ")
        assert "Cookie" not in c.session.headers


class TestCloudflareBackoff:
    """L6: OJ.ph sits behind Cloudflare and answers 521 when the edge can't reach
    the origin. A 1-2-4s ladder re-arrives while it's still down."""

    def test_521_backs_off_at_least_fifteen_seconds(self, monkeypatch):
        import requests
        slept = []
        monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

        class Resp:
            status_code = 521
            headers = {}
            text = ""

        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp())
        c = OJClient(delay=1.0, max_retries=1)
        with pytest.raises(Exception) as exc:
            c.get("http://x/job/1")
        assert exc.value.status == 521, "the caller needs the status to spot a storm"
        assert slept and max(slept) >= 15.0

    def test_500_keeps_the_short_ladder(self, monkeypatch):
        import requests
        slept = []
        monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

        class Resp:
            status_code = 500
            headers = {}
            text = ""

        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp())
        c = OJClient(delay=1.0, max_retries=1)
        with pytest.raises(Exception):
            c.get("http://x/job/1")
        assert max(slept) < 15.0
