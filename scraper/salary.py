"""
scraper/salary.py — free-text OJ.ph salary strings → honest figures.

Ported from ojph-cleaner/salary.js, which is itself a corrected port of this
file. The extension's header named the bugs this version had ("the sibling
repo's scraper/salary.py, with two corrections the live data forced") and the
corrections were never brought back. They are now, and the corpus in
tests/fixtures/salary_cases.json is shared by both repos so they cannot drift
again.

The rules, all measured on live listings:
  - a currency CODE wins over a symbol: `AUD $15-25/hour` is Australian dollars,
    `$10 - $13 CAD per hour` is CAD, `SGD $1500` is SGD; only a markerless
    number falls back to PHP, which is this board's convention
  - a bare number is read by magnitude: 1-2 digits = hourly USD, 3-4 = monthly
    USD, 5+ = monthly PHP. A 3-4 digit figure is never read as hourly: no
    employer pays ₱1,000-₱9,999 an hour
  - a month is claimed only when the listing supports it: stated `N hours/week`
    (or the listing's own HOURS PER WEEK), or "full time" → 40 h/week labelled
    as an assumption. Part-time with no stated hours, a day rate, or a piece
    rate get NO month — a guessed month is a wrong month
  - a trailing monthly figure beats a leading hourly one
    (`PHP 240/hour, approx. PHP 40,000/mo` → ₱40,000)
  - comma groups are read as the poster grouped them: 1-2 digits = decimal
    (`7,5` = 7.5), 3+ = thousands (`35,000` = 35000), and a mis-grouped
    `35,0000` is ₱350,000, not "$35/hour"

FX policy is unchanged: normalization always uses LIVE rates (Frankfurter =
ECB daily reference). No fallback/default rate — a currency with no live rate
normalizes to NULL. Outdated money is worse than no money.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass

import requests

WEEKS_PER_MONTH = 4.33
FULL_TIME_HOURS = 40
BARE_HOURLY_MAX = 99  # a bare 1-2 digit figure is an hourly rate on this board
BARE_PHP_MIN = 10000  # a bare monthly figure below this is not pesos

FX_API = "https://api.frankfurter.dev/v1/latest"  # ECB daily reference rates
FX_REFRESH_SECONDS = 86400  # a live rate is good for 24h, then it must be re-fetched
FX_RETRY_AFTER = 3600  # after a failed fetch, don't hammer the API every row

_FX_LOCK = threading.Lock()
_FX_OK: dict[str, tuple[float, float]] = {}  # CUR -> (rate PHP per 1 CUR, fetched_at)
_FX_FAIL: dict[str, float] = {}  # CUR -> last failed fetch at

# The lookbehind allows a digit in front ("400USD/mo", "35,000PHP") but not a letter,
# so "plus" is not "us".
CODE_RE = re.compile(r"(?<![a-z])(php|peso|usd|us|eur|gbp|aud|cad|sgd|nzd|jpy|yen)(?![a-z])", re.I)
# (?![a-z]), not \b: a code butted against a digit is still a code — "Php40,000" states pesos,
# and \b has no boundary between "php" and "40".
CODE_MAP = {"php": "PHP", "peso": "PHP", "usd": "USD", "us": "USD", "eur": "EUR",
            "gbp": "GBP", "aud": "AUD", "cad": "CAD", "sgd": "SGD", "nzd": "NZD", "jpy": "JPY",
            "yen": "JPY"}
SYMBOLS = ((re.compile("₱"), "PHP"), (re.compile("€"), "EUR"), (re.compile("£"), "GBP"),
           (re.compile("¥"), "JPY"), (re.compile(r"\$"), "USD"))
TIME_UNIT_RE = re.compile(r"annual|year|hour|/hr|per hr|week|day")
MONTHLY_RE = re.compile(r"month|/m\b|/mo\b")
# A `+/-` or a free-standing `/` is punctuation, not a per-unit marker: the slash form
# only counts when a unit word follows it, or "$5+/- per hour" loses its month.
PIECE_RATE_RE = re.compile(r"\bper\s+(?!\s*(?:hour|hr|day|week|month|year|annum)s?\b)"
                           r"|/(?=[a-z])(?!\s*(?:per\s+)?(?:hr|hour|day|week|month|mo|m|year)s?\b)")
# A thousands comma is not a separator between figures ("Starting at $1,200")
SPLIT_RE = re.compile(r";|\+|,(?!\d{3}(?!\d))|\band\b")


@dataclass(frozen=True)
class Salary:
    """One parsed salary. `min`/`max` are monthly in `currency` and only mean
    anything when `monthly` is true; `raw_min`/`raw_max` are the posted figures."""

    min: float
    max: float
    raw_min: float
    raw_max: float
    currency: str
    explicit: bool
    assumed_currency: bool
    per_unit: bool
    unit: str | None  # hour | hour? | day | week | month | year ('hour?' = magnitude guess)
    hours: float | None
    hours_basis: str  # stated | full-time | part-time | unstated
    monthly: bool


def _num(tok: str) -> float:
    """'40,000' → 40000.0, '7,5' → 7.5, '35,0000' → 350000.0: comma groups are
    read as the poster grouped them, not by counting digits from the wrong end."""
    out = re.sub(r",(\d{1,2})(?!\d)", lambda m: "." + m.group(1), tok)
    try:
        return float(out.replace(",", ""))
    except ValueError:  # degenerate multi-comma token
        return float(tok.replace(",", "").replace(".", ""))


def hours_per_week_from(text: str | None) -> tuple[float | None, str]:
    """The listing's own weekly hours, or the 40 h a "full time" listing states.

    Part-time is checked BEFORE full-time and wins: a part-time listing whose
    prose mentions "full time availability" must not be handed a 40-hour month
    (this failed live: $6/hour → ₱60,223/mo).
    """
    s = (text or "").lower()
    m = (re.search(r"(\d{1,3})\s*(?:hours?|hrs?)\s*(?:/|per\s+)?\s*(?:week|wk)", s)
         or re.search(r"(?:hours?|hrs?)\s*(?:per|/)?\s*(?:week|wk)\s*[:\-]?\s*(\d{1,3})", s))
    if m and 0 < int(m.group(1)) <= 80:
        return float(m.group(1)), "stated"
    if re.search(r"part[\s-]?time", s):
        return None, "part-time"
    if re.search(r"full[\s-]?time", s):
        return float(FULL_TIME_HOURS), "full-time"
    return None, "unstated"


def _currency_of(s: str) -> str | None:
    """A stated currency marker, or None. Codes are checked before symbols:
    `AUD $15-25/hour` is Australian dollars, not US."""
    m = CODE_RE.search(s)
    if m:
        return CODE_MAP[m.group(1).lower()]
    for rx, cur in SYMBOLS:
        if rx.search(s):
            return cur
    return None


def parse_salary(text: str | None, hours_per_week: float | None = None,
                 ) -> Salary | None:
    """Parse one salary string. Returns None when the string states no money."""
    if not text or not isinstance(text, str):
        return None

    s = text.lower()
    s = re.sub(r"\d+\s*%", "", s)  # commission percentages are not prices
    # Parenthetical asides carry comparison figures ("$700/mo ($175/week)",
    # "($20 per store x 5 stores)"), not the salary — drop them, but only when
    # a number still remains outside ("($2.00-$3.00) hourly").
    s2 = re.sub(r"\([^)]*\)", " ", s)
    if re.search(r"\d", s2):
        s = s2

    hours, basis = hours_per_week_from(s)
    if hours_per_week is not None and hours_per_week > 0:
        hours, basis = float(hours_per_week), "stated"

    # A monthly figure stated anywhere is the salary; hourly/comparison figures
    # are how it was built. "PHP 240/hour, approx. PHP 40,000/mo" → 40,000.
    parts = SPLIT_RE.split(s)
    if len(parts) > 1:
        monthly_parts = [p for p in parts if MONTHLY_RE.search(p) and re.search(r"\d", p)]
        if monthly_parts:
            s = " ".join(monthly_parts)

    numbers = [_num(m.group(0)) for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", s)]
    numbers = [n for n in numbers if n > 0][:2]
    if not numbers:
        return None

    lo, hi = min(numbers), max(numbers)
    cur = _currency_of(s)
    marks_unit = bool(TIME_UNIT_RE.search(s) or MONTHLY_RE.search(s))
    unit, factor, monthly = None, 1.0, True

    if marks_unit:
        if re.search(r"annual|year", s):
            unit, factor = "year", 1 / 12
        elif re.search(r"hour|/hr|per\s+hr", s):
            unit = "hour"
        elif re.search(r"week", s):
            unit, factor = "week", WEEKS_PER_MONTH
        elif re.search(r"day", s):
            unit = "day"
        else:
            unit = "month"
    else:
        unit = "hour?" if hi <= BARE_HOURLY_MAX else "month"

    if unit in ("hour", "hour?"):
        if hours:
            factor = hours * 4  # the same 4-week month as the old 160 h convention
        else:
            monthly = False  # no stated hours → no month, not even a full-time one
    elif unit == "day":
        monthly = False  # a day rate has no monthly equivalent without days/week

    # A stated marker always wins. Otherwise an hourly/daily/weekly rate is pesos
    # (the board's convention), while a monthly figure is read by magnitude — the
    # one case where "no marker means pesos" produces money no employer pays.
    assumed = False
    if not cur:
        assumed = True
        cur = "USD" if ((not marks_unit or unit == "month") and hi < BARE_PHP_MIN) else "PHP"

    round10 = lambda n: round(n * factor * 10) / 10  # noqa: E731
    per_unit = bool(PIECE_RATE_RE.search(s))
    return Salary(min=round10(lo), max=round10(hi), raw_min=lo, raw_max=hi, currency=cur,
                  explicit=bool(cur), assumed_currency=assumed, per_unit=per_unit, unit=unit,
                  hours=hours, hours_basis=basis, monthly=monthly and not per_unit)


def _convert(amount: tuple[float, float], currency: str, rates: dict) -> tuple[float, float] | None:
    if currency == "PHP":
        return amount
    rate = rates.get(currency.upper())
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return None
    # half-up, like every money round in this repo: Python's round() is half-to-even,
    # which made ₱78,422.50 into 78422 while the extension's copy said 78423
    half_up = lambda n: math.floor(n * rate + 0.5)  # noqa: E731
    return half_up(amount[0]), half_up(amount[1])


def monthly_php(s: Salary | None, fx: dict | None = None) -> tuple[float | None, float | None]:
    """Monthly PHP, or (None, None) when no month can honestly be computed."""
    if not s or s.per_unit or not s.monthly:
        return (None, None)
    out = _convert((s.min, s.max), s.currency, _rates(fx, s.currency))
    return out or (None, None)


def rate_php(s: Salary | None, fx: dict | None = None) -> tuple[float | None, float | None]:
    """The posted per-unit amount in PHP: per hour, per day, or per item.

    A month/year/week rate is what `monthly_php` already reports, so it gets no
    separate rate. `hour?` — a bare number that states no unit at all — gets none
    either, because there is no unit to state it per.
    """
    if not s or (s.unit == "hour?" and not s.per_unit):
        return (None, None)
    if not s.per_unit and s.unit not in ("hour", "day"):
        return (None, None)
    out = _convert((s.raw_min, s.raw_max), s.currency, _rates(fx, s.currency))
    return out or (None, None)


def _rates(fx: dict | None, currency: str) -> dict:
    rates = {"PHP": 1.0, **{str(k).upper(): v for k, v in (fx or {}).items()}}
    if currency.upper() != "PHP" and currency.upper() not in rates:
        rates = {"PHP": 1.0, **{str(k).upper(): v for k, v in fetch_fx_to_php([currency]).items()}}
    return rates


def fetch_fx_to_php(currencies: list[str] | None, timeout: int = 10) -> dict[str, float]:
    """Live rates: PHP per 1 unit of each requested currency (Frankfurter/ECB).

    A rate is cached for FX_REFRESH_SECONDS (the server refreshes daily); once
    it is older than that it MUST be re-fetched. A failed fetch omits the
    currency — it is never approximated and never served stale. PHP is identity
    and needs no fetch. Returns only what is actually live.
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


def normalize_to_php(mn: float | None, mx: float | None, cur: str | None,
                     fx: dict | None = None) -> tuple[float | None, float | None]:
    """Convert a (min, max) pair to PHP with live rates. No live rate → (None, None)."""
    if mn is None or mx is None or not cur:
        return (None, None)
    out = _convert((mn, mx), cur, _rates(fx, cur))
    return out or (None, None)
