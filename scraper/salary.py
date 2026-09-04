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

# ponytail: heuristic ceiling — exotic strings (multi-currency, "above range")
# yield None/None, which is the safe failure (nothing to sort by).
"""

import re

HOURS_PER_MONTH = 160
DAYS_PER_MONTH = 30
WEEKS_PER_MONTH = 4.33


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Return (min, max, currency) in monthly terms, or (None, None, None)."""
    if not text or not isinstance(text, str):
        return (None, None, None)

    s = text.lower()
    s = re.sub(r"\d+\s*%", "", s)  # commission percentages are not prices

    if "php" in s or "₱" in s or "peso" in s:
        cur = "PHP"
    elif "usd" in s or "$" in s:
        cur = "USD"
    else:
        cur = None

    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]
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
    else:
        factor = 1.0
        if hi <= 100:  # no unit, tiny numbers → hourly on this board
            factor = HOURS_PER_MONTH

    if cur is None:
        cur = "PHP"

    return (round(lo * factor, 1), round(hi * factor, 1), cur)
