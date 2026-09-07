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
