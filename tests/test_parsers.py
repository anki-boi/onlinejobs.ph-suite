"""
tests/test_parsers.py — Parser tests using saved HTML fixtures.

Fixtures are stored in tests/fixtures/ as .html files.
For now we test with synthetic HTML that matches the real structure.
"""

import pytest

from scraper.parsers import (
    JobDetail,
    JobStub,
    extract_job_id,
    get_total_results,
    parse_job_detail,
    parse_search_results,
)


# ── extract_job_id ──────────────────────────────────────────────────────────

class TestExtractJobId:
    def test_full_url(self):
        assert extract_job_id("https://www.onlinejobs.ph/jobseekers/job/digital-social-media-marketing-director-1701214") == 1701214

    def test_relative_url(self):
        assert extract_job_id("/jobseekers/job/python-developer-jr-1701282") == 1701282

    def test_no_id(self):
        assert extract_job_id("https://www.onlinejobs.ph/about") is None

    def test_empty(self):
        assert extract_job_id("") is None

    def test_none(self):
        assert extract_job_id(None) is None

    def test_numeric_only(self):
        # A URL that's just a number
        assert extract_job_id("/jobseekers/job/12345") is None


# ── get_total_results ───────────────────────────────────────────────────────

class TestGetTotalResults:
    def test_found(self):
        html = '<script>var dataLayer = [{"search_result_count":2143}]</script>'
        assert get_total_results(html) == 2143

    def test_not_found(self):
        html = "<html><body>no data layer</body></html>"
        assert get_total_results(html) is None

    def test_zero(self):
        html = '{"search_result_count":0}'
        assert get_total_results(html) == 0


# ── parse_search_results ────────────────────────────────────────────────────

# Minimal fixture matching the real OJ.ph structure
SEARCH_HTML = """
<html><body>
<div class="job-list">
  <div class="jobpost-cat-box latest-job-post">
    <h4>Digital / Social Media Marketing Director <span class="badge">Any</span></h4>
    <p data-temp="2026-08-26 08:29:32"><em>Company A</em> •&nbsp;&nbsp; Posted on 2026-08-26 08:29:32</p>
    <a class="joblink" href="/jobseekers/job/digital-social-media-marketing-director-1701214">Apply</a>
    <dd class="col">$1500-3500 /month</dd>
    <div class="desc">
      <a>Location: Remote
Hours: 20-30 hours/week
Compensation: $500-1000/month</a>
    </div>
    <div class="job-tag">
      <a class="badge">Marketing</a>
      <a class="badge">Social Media</a>
    </div>
  </div>

  <div class="jobpost-cat-box latest-job-post">
    <h4>Python Developer (Jr) <span class="badge">Part</span></h4>
    <p data-temp="2026-08-25 14:30:00"><em>Company B</em> •&nbsp;&nbsp; Posted on 2026-08-25 14:30:00</p>
    <a class="joblink" href="/jobseekers/job/python-developer-jr-1701282">Apply</a>
    <dd class="col">$600 /month</dd>
    <div class="desc">
      <a>Location: Remote
Hours: 10 hours/week
Compensation: $600/month</a>
    </div>
    <div class="job-tag">
      <a class="badge">Python</a>
      <a class="badge">Django</a>
    </div>
  </div>
</div>
</body></html>
"""


class TestParseSearchResults:
    def test_count(self):
        stubs = parse_search_results(SEARCH_HTML)
        assert len(stubs) == 2

    def test_first_job_fields(self):
        stubs = parse_search_results(SEARCH_HTML)
        j = stubs[0]
        assert j.job_id == 1701214
        assert "1701214" in j.job_url
        assert j.title == "Digital / Social Media Marketing Director"
        assert j.work_type == "Any"
        assert j.company == "Company A"
        assert j.posted_date == "2026-08-26 08:29:32"
        assert j.salary == "$1500-3500 /month"
        assert j.location == "Remote"
        assert j.hours == "20-30 hours/week"
        assert "Marketing" in j.skills
        assert "Social Media" in j.skills

    def test_second_job_fields(self):
        stubs = parse_search_results(SEARCH_HTML)
        j = stubs[1]
        assert j.job_id == 1701282
        assert j.work_type == "Part"
        assert j.company == "Company B"
        assert j.salary == "$600 /month"

    def test_no_badge(self):
        html = """
        <div class="jobpost-cat-box latest-job-post">
          <h4>Plain Title</h4>
          <p data-temp="2026-01-01 00:00:00">NoCompany • Posted on 2026-01-01</p>
          <a href="/jobseekers/job/plain-99999">Apply</a>
        </div>
        """
        stubs = parse_search_results(html)
        assert len(stubs) == 1
        assert stubs[0].title == "Plain Title"
        assert stubs[0].work_type is None

    def test_empty_page(self):
        stubs = parse_search_results("<html><body><p>No jobs</p></body></html>")
        assert stubs == []

    def test_url_normalisation(self):
        stubs = parse_search_results(SEARCH_HTML)
        assert stubs[0].job_url.startswith("https://")
        assert "/jobseekers/job/" in stubs[0].job_url


# ── parse_job_detail ────────────────────────────────────────────────────────

DETAIL_HTML = """
<html><head></head><body>
<div id="job-description-page">
  <div id="job-description" data-jobid="1701282">
    <h1 class="job__title" data-jobid="1701282">Python Developer (Jr) / Senior Backend Engineer</h1>
    <h3 class="job__logo">
      <img src="/employer_logos/1234/logo.png" alt="">Some Company Inc
    </h3>

    <div class="card">
      <dl>
        <dt><h3 class="fs-12">TYPE OF WORK</h3></dt>
        <dd><p class="fs-18">Part Time</p></dd>
        <dt><h3 class="fs-12">WAGE / SALARY</h3></dt>
        <dd><p class="fs-18">$600/month</p></dd>
        <dt><h3 class="fs-12">HOURS PER WEEK</h3></dt>
        <dd><p class="fs-18">10 hours/week</p></dd>
        <dt><h3 class="fs-12">DATE UPDATED</h3></dt>
        <dd><p class="fs-18">2026-08-26 08:29:32</p></dd>
      </dl>
    </div>

    <div class="skill-tags">
      <a class="card-worker-topskill" href="/skill/python">Python</a>
      <a class="card-worker-topskill" href="/skill/django">Django</a>
      <a class="card-worker-topskill" href="/skill/rest-api">REST API</a>
    </div>

    <p>Job description text goes here. It spans multiple lines.
    Line two of the description.
    </p>
  </div>
</div>
</body></html>
"""

CLOSED_HTML = """
<html><body>
<h3 class="text-warning">⚠ This job has been closed</h3>
<div id="job-description" data-jobid="99999">
  <h1 class="job__title" data-jobid="99999">Old Job Title</h1>
  <h3 class="job__logo">
    <img src="/employer_logos/555/logo.png">Old Company
  </h3>
</div>
</body></html>
"""


class TestParseJobDetail:
    def test_basic_fields(self):
        d = parse_job_detail(DETAIL_HTML, url="/jobseekers/job/python-developer-jr-1701282")
        assert d.job_id == 1701282
        assert d.title == "Python Developer (Jr) / Senior Backend Engineer"
        assert d.company == "Some Company Inc"
        assert d.work_type == "Part Time"
        assert d.salary == "$600/month"
        assert d.hours_per_week == "10 hours/week"
        assert d.date_updated == "2026-08-26 08:29:32"
        assert d.employer_id == 1234
        assert "Python" in d.skills
        assert "Django" in d.skills
        assert "REST API" in d.skills
        assert d.description is not None
        assert "Job description text" in d.description
        assert not d.is_closed

    def test_closed_job(self):
        d = parse_job_detail(CLOSED_HTML)
        assert d.is_closed
        assert d.title == "Old Job Title"
        assert d.employer_id == 555

    def test_empty_page(self):
        d = parse_job_detail("<html><body></body></html>")
        assert d.job_id is None
        assert d.title is None

    def test_description_strips_title(self):
        """Description should not repeat the title on the first line."""
        html = """
        <div id="job-description" data-jobid="123">
          <h1 class="job__title" data-jobid="123">My Title</h1>
          <p>My Title
          Actual content here.
          </p>
        </div>
        """
        d = parse_job_detail(html)
        assert d.description
        assert not d.description.startswith("My Title")


# ── Status normalisation ────────────────────────────────────────────────────

class TestStatusValues:
    def test_valid_statuses(self):
        from db.repos.jobs import STATUSES
        assert "New" in STATUSES
        assert "Applied" in STATUSES
        assert "Hired" in STATUSES
        assert "Hidden" in STATUSES
        assert len(STATUSES) == 8
