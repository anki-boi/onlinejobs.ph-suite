"""
tests/test_salary_corpus.py — the corpus `ojph-cleaner/salary.js` and this parser
must agree on.

`tests/fixtures/salary_cases.json` is committed in BOTH repos with identical bytes
(the drift check at the bottom fails if they diverge). Each repo runs its own parser
over it, so a rule change in one repo shows up as a failing test in the other.
"""

import hashlib
import json
from pathlib import Path

import pytest

from scraper.salary import monthly_php, parse_salary

CORPUS = Path(__file__).parent / "fixtures" / "salary_cases.json"
SIBLING = Path(__file__).parents[2] / "ojph-cleaner" / "tests" / "fixtures" / "salary_cases.json"

DATA = json.loads(CORPUS.read_text(encoding="utf-8"))
CASES = DATA["cases"]
RATES = DATA["rates"]


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("case", CASES, ids=[c["text"] or "(empty)" for c in CASES])
def test_corpus_case(case):
    p = parse_salary(case["text"], case["hours"])
    if not case["parsed"]:
        assert p is None, f"{case['text']!r} should state no money"
        return
    assert p is not None, f"{case['text']!r} should parse"
    label = f"{case['text']!r} (hours={case['hours']})"
    assert p.currency == case["currency"], label
    assert p.unit == case["unit"], label
    assert p.monthly == case["monthly"], label
    assert p.per_unit == case["per_unit"], label
    assert p.assumed_currency == case["assumed_currency"], label
    assert (p.raw_min, p.raw_max) == (case["raw_min"], case["raw_max"]), label
    assert (p.min, p.max) == (case["min"], case["max"]), label
    assert monthly_php(p, RATES) == (case["php_min"], case["php_max"]), label


def test_corpus_is_the_same_file_in_both_repos():
    """The whole point of the corpus is that neither repo can drift silently."""
    if not SIBLING.exists():
        pytest.skip("ojph-cleaner is not checked out next to this repo")
    assert _digest(CORPUS) == _digest(SIBLING), (
        "tests/fixtures/salary_cases.json differs from ojph-cleaner's copy — regenerate both")


def test_corpus_covers_the_rules_that_were_wrong_here():
    """The live-damage cases this corpus exists for (sampled from jobs.db)."""
    texts = {c["text"] for c in CASES}
    for text in ("AUD 15/hr", "1250 aud", "$5-7/hr CAD", "1200 EUR monthly",
                 "3000 SGD per month", "1000", "Pay Per View", "$50-$150 per video"):
        assert text in texts, f"{text} must stay in the shared corpus"
