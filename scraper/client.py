"""
scraper/client.py — Persistent HTTP session for OnlineJobs.ph.

Handles:
  - Connection reuse (requests.Session)
  - Rate limiting (global throttle: min delay between any two requests)
  - Exponential backoff on 429 / 5xx
  - Cancellation via stop()
"""

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)


class ScrapeStopped(Exception):
    """Raised when the user requests a stop."""


class StopToken:
    """One stop flag per pipeline run (W2.8).

    The HTTP client is shared and persistent (session reuse), but stopping
    must be per run: two overlapping runs (auto + manual) stop independently.
    A run creates a token, hands it to the client via set_stop(), and its
    Stop button flips exactly that token.
    """

    def __init__(self):
        self._event = threading.Event()

    def stop(self):
        self._event.set()

    def reset(self):
        self._event.clear()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()


class RateLimitExhausted(Exception):
    """Raised after max retries on 429/5xx.

    L6 (audit): `status` carries the last HTTP code seen, so the pipeline can tell
    a Cloudflare rate-limit storm (521) from a genuinely dead page."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


DEFAULT_BASE_URL = "https://www.onlinejobs.ph"

# L6: Cloudflare's "origin is down / unavailable" family. They clear on their own;
# they are not a reason to hammer the edge with a 1-second ladder.
_CF_DOWN = {521, 522, 523, 524}
DEFAULT_API_URL = "https://api.onlinejobs.ph"


class OJClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_url: str = DEFAULT_API_URL,
        delay: float = 1.0,
        max_retries: int = 3,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        cookies: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.delay = delay
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        })
        # L2 (audit): `oj_cookies` was documented in the README, redacted in
        # /api/config and covered by a redaction test — and never sent. Scraping
        # logged-out is why employer_id is NULL on 90% of rows and why the
        # employer block (hence company) was missing. A `k=v; k2=v2` string from
        # config.local.json fixes both.
        if cookies and cookies.strip():
            self.session.headers["Cookie"] = cookies.strip()
        self._stop_token = StopToken()  # W2.8: per-run, swappable via set_stop()
        self._lock = threading.Lock()
        self._last_request = 0.0

    # ── Rate limiter ──────────────────────────────────────────────────────

    def _throttle(self):
        """Ensure at least `delay` seconds between any two requests (thread-safe)."""
        with self._lock:
            now = time.monotonic()
            wait = self._last_request + self.delay - now
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    # ── HTTP ──────────────────────────────────────────────────────────────

    def get(self, path: str, base: str | None = None) -> requests.Response:
        """
        GET with exponential backoff on 429/5xx.
        `path` is relative (e.g. "/jobseekers/jobsearch?…") or a full URL.
        `base` overrides the default base_url (use api_url for the API).
        """
        url = path if path.startswith("http") else f"{base or self.base_url}{path}"
        last_exc: Exception | None = None
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            if self._stop_token.stopped:
                raise ScrapeStopped("User requested stop")

            self._throttle()
            try:
                resp = self.session.get(url, timeout=20)
            except requests.RequestException as exc:
                last_exc = exc
                wait = (2 ** attempt) * self.delay
                log.warning("Request error (%s): %s — retrying in %ds", url, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                # Respect server-provided Retry-After (seconds or HTTP-date).
                last_status = resp.status_code
                retry_after = resp.headers.get("Retry-After")
                wait = self._parse_retry_after(retry_after) or (2 ** attempt) * self.delay
                log.warning("429 rate limit on %s — backing off %ds (attempt %d/%d)",
                            url, wait, attempt + 1, self.max_retries + 1)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                last_status = resp.status_code
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                # Respect server-provided Retry-After on 5xx too.
                retry_after = resp.headers.get("Retry-After")
                wait = self._parse_retry_after(retry_after) or (2 ** attempt) * self.delay
                if resp.status_code in _CF_DOWN:
                    # L6: Cloudflare's 52x means "edge can't reach the origin" and
                    # clears in tens of seconds. A 1-2-4s ladder just burns the
                    # retry budget and re-arrives while it's still down.
                    wait = max(wait, 15.0)
                log.warning("HTTP %d on %s — retrying in %ds", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            return resp

        raise RateLimitExhausted(
            f"Failed after {self.max_retries} retries: {url}", status=last_status
        ) from last_exc

    def post_json(self, url: str, body: dict) -> requests.Response:
        """POST JSON (for the skills API)."""
        self._throttle()
        resp = self.session.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp

    # ── Control ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_retry_after(header: str | None) -> float | None:
        """Parse a Retry-After header into seconds, or return None."""
        if not header:
            return None
        try:
            return float(header)
        except ValueError:
            pass
        # Try HTTP-date (e.g. "Wed, 21 Oct 2025 07:28:00 GMT")
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(header)
            return max(0, (dt.timestamp() - time.time()))
        except Exception:
            return None

    # ── Control ───────────────────────────────────────────────────────

    def set_stop(self, token: StopToken):
        """Attach this run's stop token (the session itself is kept)."""
        self._stop_token = token

    def stop(self):
        self._stop_token.stop()

    def reset(self):
        self._stop_token.reset()

    @property
    def stopped(self) -> bool:
        return self._stop_token.stopped
