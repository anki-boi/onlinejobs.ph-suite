"""
tests/test_salary.py — the salary parser's own rules (the shared corpus lives in
tests/test_salary_corpus.py).

The policy under test: a monthly figure is claimed only when the listing gives a
per-month/week/year amount, or an hourly rate plus stated weekly hours (or "full
time", which means 40 h/week). There is no fallback for unstated hours — not 40,
and not 20 for "Part Time", which states no number at all.
"""

from scraper.salary import (FULL_TIME_HOURS, hours_per_week_from, monthly_php,
                            normalize_to_php, parse_salary, rate_php)

RATES = {"USD": 62.738, "EUR": 61.2, "AUD": 42.1, "CAD": 45.9, "SGD": 48.4, "GBP": 58.4}
FT = FULL_TIME_HOURS


def monthly(text, hours=None):
    p = parse_salary(text, hours)
    return None if not p or not p.monthly else (p.min, p.max, p.currency)


def test_stated_monthly_and_weekly():
    assert monthly("40000-50000php") == (40000.0, 50000.0, "PHP")
    assert monthly("$800-$1200/mo") == (800.0, 1200.0, "USD")
    assert monthly("$200/Week + 5% commission") == (866.0, 866.0, "USD")
    assert monthly("Circa Annual Salary - US$6000") == (500.0, 500.0, "USD")


def test_hourly_needs_hours():
    assert monthly("$5/hour") is None  # no hours stated → no month may be claimed
    assert monthly("$5/hour", 20) == (400.0, 400.0, "USD")
    assert monthly("$5/hour", FT) == (800.0, 800.0, "USD")
    assert monthly("140 -175/ per hour", 20) == (11200.0, 14000.0, "PHP")


def test_day_rate_is_never_a_month():
    p = parse_salary("Php 1000/day")
    assert p.unit == "day" and not p.monthly
    assert monthly("Php 1000/day", FT) is None  # days/week is unknown, even full time
    assert rate_php(p, {}) == (1000, 1000)  # the posted rate is still true on its own


def test_piece_rate_gets_no_month():
    for text in ("$5 per entry", "$2/article", "$50-$150 per video", "$5 per account verified"):
        p = parse_salary(text, FT)
        assert p.per_unit and not p.monthly, text
        assert monthly_php(p, RATES) == (None, None), text
    assert parse_salary("Pay Per View") is None  # states no number at all
    assert parse_salary("3/Hours").per_unit is False  # plural "Hours" is a time unit
    assert parse_salary("$5+/- per hour").per_unit is False  # "+/-" is punctuation


def test_currency_codes_beat_symbols_and_markers_are_honest():
    assert parse_salary("$10 - $13 CAD per hour", FT).currency == "CAD"
    assert parse_salary("SGD $1500 to $2,000 excluding bonuses").currency == "SGD"
    assert parse_salary("1000€ /m").currency == "EUR"
    assert parse_salary("400USD/mo").currency == "USD"
    assert parse_salary("140 -175 per hour plus benefits").currency == "PHP"
    assert parse_salary("42000").assumed_currency is True
    assert parse_salary("Php 42000").assumed_currency is False
    assert parse_salary("Php40,000.00 - Php50,000.00").assumed_currency is False


def test_bare_numbers_are_read_by_magnitude():
    assert monthly("6.00") is None and parse_salary("6.00").unit == "hour?"
    assert monthly("1000") == (1000.0, 1000.0, "USD")  # 3-4 digits = monthly dollars
    assert monthly("35000") == (35000.0, 35000.0, "PHP")  # 5+ digits = monthly pesos
    assert monthly("€450-550") == (450.0, 550.0, "EUR")  # a stated marker always wins


def test_comma_groups_are_read_as_the_poster_grouped_them():
    assert parse_salary("7,5").min == 7.5
    assert parse_salary("1,500").min == 1500.0
    assert parse_salary("1,2345").min == 12345.0
    p = parse_salary("35,0000 - 40,0000")  # a live mis-typed ₱350,000/month card
    assert (p.min, p.max, p.currency) == (350000.0, 400000.0, "PHP")


def test_trailing_monthly_beats_leading_hourly():
    assert monthly("PHP 240/hour, approx. PHP 40,000/mo") == (40000.0, 40000.0, "PHP")


def test_hours_basis_precedence():
    assert hours_per_week_from("20 hours per week") == (20.0, "stated")
    assert hours_per_week_from("Hours per week: 30") == (30.0, "stated")
    assert hours_per_week_from("Part Time") == (None, "part-time")
    assert hours_per_week_from("Part Time — some full time availability needed") == (None, "part-time")
    assert hours_per_week_from("Full Time") == (40.0, "full-time")
    assert hours_per_week_from("no schedule given") == (None, "unstated")


def test_no_money_is_no_number():
    for text in ("DOE", "", None, "Negotiable", "Commensurate with experience"):
        assert parse_salary(text) is None, text


def test_fx_is_live_or_absent_never_guessed():
    p = parse_salary("$880")
    assert monthly_php(p, {}) == (None, None)  # no rate → no figure, never a guess
    assert monthly_php(p, RATES) == (55209, 55209)
    assert monthly_php(parse_salary("Php 30,000"), {}) == (30000, 30000)
    cad = parse_salary("$10 - $13 CAD per hour", FT)
    assert monthly_php(cad, {"USD": 62.738}) == (None, None)  # a CAD card needs a CAD rate
    assert normalize_to_php(1000, 2000, "USD", RATES) == (62738, 125476)
    assert normalize_to_php(None, None, "USD", RATES) == (None, None)
