"""
tests/test_salary.py — parse_salary: free-text OJ.ph salary strings →
(min, max, currency) in monthly terms. Pure function; every case is a
format observed in the real jobs.db.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.salary import parse_salary


def test_php_range():
    assert parse_salary("40000-50000php") == (40000.0, 50000.0, "PHP")


def test_php_range_with_currency_word():
    assert parse_salary("Php40,000.00 - Php50,000.00") == (40000.0, 50000.0, "PHP")


def test_usd_monthly_range():
    assert parse_salary("$800-$1200/mo") == (800.0, 1200.0, "USD")


def test_usd_monthly_to():
    assert parse_salary("$6 to $15/hr") == (960.0, 2400.0, "USD")  # hourly × 160


def test_usd_hourly_word():
    assert parse_salary("12 USD per hour") == (1920.0, 1920.0, "USD")  # × 160


def test_usd_weekly():
    min_v, max_v, cur = parse_salary("$200/Week + 5% commission")
    assert cur == "USD"
    assert 850 <= min_v <= 880 and 850 <= max_v <= 880  # weekly × ~4.33


def test_plain_big_number_is_php_monthly():
    assert parse_salary("42000") == (42000.0, 42000.0, "PHP")


def test_tiny_numbers_are_hourly_php():
    # 11-17 with no unit and no currency: on OJ.ph that's an hourly rate
    min_v, max_v, cur = parse_salary("11-17")
    assert cur == "PHP"
    assert 1700 <= min_v <= 1800 and 2700 <= max_v <= 2800


def test_annual_usd():
    assert parse_salary("Circa Annual Salary - US$6000") == (500.0, 500.0, "USD")  # /12


def test_per_day():
    assert parse_salary("Php 1000/day") == (30000.0, 30000.0, "PHP")  # × 30


def test_single_value():
    assert parse_salary("$400") == (400.0, 400.0, "USD")


def test_no_numbers():
    assert parse_salary("DOE") == (None, None, None)
    assert parse_salary("") == (None, None, None)
    assert parse_salary(None) == (None, None, None)


def test_starting_at():
    assert parse_salary("Starting at $1,200 USD/month") == (1200.0, 1200.0, "USD")


def test_mojibake_dash():
    # real data: en-dash eaten by the site's encoding → '$350$380/mo'
    assert parse_salary("$350$380/mo (Total Package)") == (350.0, 380.0, "USD")
