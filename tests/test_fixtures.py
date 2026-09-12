"""
tests/test_fixtures.py — W3.1: parser tests against the real captured page
corpus in scraper/fixtures/ (captured 2026-09-13, 1 req/s). No network here —
live drift is checked by tools/check_fixtures.py outside pytest.
"""

from pathlib import Path

from scraper.parsers import get_total_results, parse_job_detail, parse_search_results

FX = Path(__file__).parent.parent / "scraper" / "fixtures"


def _read(name: str) -> str:
    p = FX / name
    assert p.exists(), f"missing fixture {name} — run the capture (see tools/check_fixtures.py header)"
    return p.read_text(encoding="utf-8")


class TestSearchResultsFixture:
    def test_parses_real_search_page(self):
        stubs = parse_search_results(_read("search_results.html"))
        assert len(stubs) >= 2
        for s in stubs:
            assert s.job_id is not None, f"stub without job_id: {s.job_url}"
            assert s.title
            assert s.job_url.startswith("https://")

    def test_result_count_datalayer(self):
        n = get_total_results(_read("search_results.html"))
        assert n is not None and n > 0

    def test_first_stub_fields(self):
        stubs = parse_search_results(_read("search_results.html"))
        s = stubs[0]
        assert s.job_id == 1729186
        assert "AI Video Editor" in s.title
        assert s.salary  # "Negotiable based on experience 5-10 USDh"
        assert s.posted_date  # p[data-temp]
        assert s.work_type == "Any"
        # live markup: company name is the logo img's alt (W3.2 fallback)
        assert s.company


class TestJobDetailFixture:
    def test_open_job(self):
        d = parse_job_detail(_read("job_detail.html"),
                             "https://www.onlinejobs.ph/jobseekers/job/ai-video-editor-direct-response-e-commerce-ads-uk-1729186")
        assert d.job_id == 1729186
        assert "AI Video Editor" in (d.title or "")
        assert d.company
        assert d.description and len(d.description) > 100
        assert not d.is_closed

    def test_closed_410_page(self):
        d = parse_job_detail(_read("job_closed.html"),
                             "https://www.onlinejobs.ph/jobseekers/job/x-1662908")
        assert d.is_closed
        assert "no longer posted" in (d.close_reason or "").lower()


class TestJob404Fixture:
    def test_404_page_classified_gone(self):
        d = parse_job_detail(_read("job_404.html"),
                             "https://www.onlinejobs.ph/jobseekers/job/this-job-does-not-exist-9999999")
        assert d.is_closed  # 404 soft page: "Oops, we lost you there."
        assert d.title is None  # no job content on a 404


class TestOldMarkupStillParses:
    """W3.2 acceptance: a page with the OLD markup (company in p[data-temp]
    text) must still parse ≥80% of fields through the fallback chain."""

    OLD_MARKUP = """
    <div class="jobpost-cat-box latest-job-post">
      <h4>Copywriter &amp; Content Creator <span class="badge">Part</span></h4>
      <p data-temp="2026-07-01 10:00:00"><em>OldStyle Inc</em> &bull;&nbsp;&nbsp; Posted on 2026-07-01 10:00:00</p>
      <a class="joblink" href="/jobseekers/job/copywriter-content-creator-1000001">Apply</a>
      <dd class="col">$800 /month</dd>
      <div class="desc">
        <a>Location: Remote
        Hours: 20 hours/week
        Compensation: $800/month</a>
      </div>
      <div class="job-tag"><a class="badge">Copywriting</a></div>
    </div>
    """

    def test_old_markup_fields(self):
        from scraper.parsers import MISSING_FIELDS as MF
        MF.clear()
        stubs = parse_search_results(self.OLD_MARKUP)
        assert len(stubs) == 1
        s = stubs[0]
        fields = [s.job_id, s.job_url, s.title, s.work_type, s.company,
                  s.posted_date, s.salary, s.location, s.hours, s.skills]
        missing = sum(v is None or v == "" or v == [] for v in fields)
        pct = 1 - missing / len(fields)
        assert pct >= 0.8, f"only {pct:.0%} of fields parsed: " \
                           f"missing={[n for n, v in zip(fields, fields) if not v]}"
        # company must have come from the old-style <em>, not the img alt
        assert s.company == "OldStyle Inc"
