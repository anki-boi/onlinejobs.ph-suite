"""
scraper/salary.py — parse free-text OJ.ph salary strings into monthly
(min, max, currency).

Formats seen in the wild:
  "40000-50000php"  "Php40,000.00 - Php50,000.00"  "$800-$1200/mo"
  "12 USD per hour" "$200/Week + 5% commission"    "$2.50$3.25/hour"
  "42000"           "11-17"                         "Starting at $1,200 USD/month"

Heuristics (deliberate, with known ceilings):
  - no unit + small numbers (<= 100) → hourly × 160 (on OJ.ph that's what they are)
  - no currency marker → PHP (this is a Philippine board)
  - weekly × 4.33, daily × 30, annual ÷ 12
  - percentages stripped before number extraction ("5% commission" is not a price)

FX policy: normalization always uses LIVE rates (Frankfurter = ECB daily
reference rates). There is no fallback/default rate: if a live rate can't
be fetched, the salary normalizes to NULL. Outdated money is worse than no
money — a stale ₱ number is a wrong number.

# ponytail: heuristic ceiling — exotic strings (multi-currency, "above range")
# yield None/None, which is the safe failure (nothing to sort by).
"""

import re
import threading
import time

import requests

HOURS_PER_MONTH = 160
DAYS_PER_MONTH = 30
WEEKS_PER_MONTH = 4.33

FX_API = "https://api.frankfurter.dev/v1/latest"  # ECB daily reference rates
FX_REFRESH_SECONDS = 86400  # a live rate is good for 24h, then it must be re-fetched
FX_RETRY_AFTER = 3600  # after a failed fetch, don't hammer the API every row

_FX_LOCK = threading.Lock()
_FX_OK: dict[str, tuple[float, float]] = {}  # CUR -> (rate PHP per 1 CUR, fetched_at)
_FX_FAIL: dict[str, float] = {}  # CUR -> last failed fetch at


def _num(tok: str) -> float:
    """'40,000' → 40000.0 (thousands separator: comma + exactly 3 digits);
    '7,5' → 7.5 (PH decimal comma: comma + 1-2 digits)."""
    try:
        return float(re.sub(r",(\d+)",
                            lambda m: m.group(1) if len(m.group(1)) == 3 else "." + m.group(1),
                            tok))
    except ValueError:  # degenerate multi-comma token: old behavior
        return float(tok.replace(",", ""))


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Return (min, max, currency) in monthly terms, or (None, None, None)."""
    if not text or not isinstance(text, str):
        return (None, None, None)

    s = text.lower()
    s = re.sub(r"\d+\s*%", "", s)  # commission percentages are not prices
    # Parenthetical asides carry comparison figures ("$700/mo ($175/week)",
    # "($20 per store x 5 stores)"), not the salary — drop them, but only
    # when a number still remains outside ("($2.00-$3.00) hourly").
    s2 = re.sub(r"\([^)]*\)", " ", s)
    if re.search(r"\d", s2):
        s = s2

    if "php" in s or "₱" in s or "peso" in s:
        cur = "PHP"
    elif "usd" in s or "$" in s:
        cur = "USD"
    else:
        cur = None

    nums = [_num(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]
    nums = [n for n in nums[:2] if n > 0]
    if not nums:
        return (None, None, None)

    lo = hi = nums[0]
    if len(nums) == 2:
        lo, hi = sorted(nums)

    if "annual" in s or "year" in s:
        factor = 1.0 / 12
    elif "hour" in s or "/hr" in s or " per hr" in s:
        factor = HOURS_PER_MONTH
    elif "week" in s:
        factor = WEEKS_PER_MONTH
    elif "day" in s:
        factor = DAYS_PER_MONTH
    elif "month" in s:
        factor = 1.0  # explicit monthly: never apply the tiny=hourly guess
    else:
        factor = 1.0
        if hi <= 100:  # no unit, tiny numbers → hourly on this board
            factor = HOURS_PER_MONTH

    if cur is None:
        cur = "PHP"

    return (round(lo * factor, 1), round(hi * factor, 1), cur)


def fetch_fx_to_php(currencies: list[str] | None, timeout: int = 10) -> dict[str, float]:
    """Live rates: PHP per 1 unit of each requested currency (Frankfurter/ECB).

    A rate is cached for FX_REFRESH_SECONDS (the server refreshes daily);
    once it's older than that it MUST be re-fetched. A failed fetch omits the
    currency — it is never approximated and never served stale. PHP is
    identity and needs no fetch. Returns only what is actually live.
    """
    out: dict[str, float] = {}
    wanted = {c.upper() for c in (currencies or []) if c}
    now = time.time()
    with _FX_LOCK:
        for cur in sorted(wanted):
            if cur == "PHP":
                out[cur] = 1.0
                continue
            hit = _FX_OK.get(cur)
            if hit is not None and now - hit[1] <= FX_REFRESH_SECONDS:
                out[cur] = hit[0]
                continue
            fail_at = _FX_FAIL.get(cur)
            if fail_at is not None and now - fail_at < FX_RETRY_AFTER:
                continue  # recently failed — don't hammer the API per row
            try:
                r = requests.get(FX_API, params={"from": cur, "to": "PHP"}, timeout=timeout)
                r.raise_for_status()
                rate = float(r.json()["rates"]["PHP"])
            except Exception:
                _FX_FAIL[cur] = now
                continue
            _FX_FAIL.pop(cur, None)
            _FX_OK[cur] = (rate, now)
            out[cur] = rate
    return out


def normalize_to_php(
    mn: float | None, mx: float | None, cur: str | None,
    fx: dict | None = None,
) -> tuple[float | None, float | None]:
    """Monthly (min, max) converted to PHP using live rates.

    `fx` maps currency → rate (case-insensitive keys); fx=None fetches live
    rates. PHP is the identity rate. No (live) rate for the currency →
    (None, None): never approximate money.
    """
    if mn is None or mx is None or not cur:
        return (None, None)
    if fx is None:
        fx = fetch_fx_to_php([cur])
    rates = {"PHP": 1.0, **{k.upper(): v for k, v in (fx or {}).items()}}
    rate = rates.get(cur.upper())
    # A rate may not be numeric (bad API payload / hand-edit) — treat as no rate
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return (None, None)
    return (round(mn * rate, 2), round(mx * rate, 2))
