"""
X-D: the flags the dashboard already knew and never showed.

off_platform — the listing wants the application off-site (and a tool name only
counts inside a sentence that is also asking, because "monitor Telegram accounts"
is job content, not an application route).
over_40h     — more than a 40-hour week.
superseded_by — this row is the older copy of a repost.
"""

import pytest

from db.repos import jobs as job_repo
from scraper.offplatform import off_platform, over_40_hours


@pytest.fixture
def conn(tmp_db):
    return tmp_db


# ── off-platform asks ────────────────────────────────────────────────────────

def test_a_tool_mentioned_as_job_content_is_not_a_warning():
    assert off_platform("Help monitor Telegram accounts and reply to DMs daily.") == ""


def test_a_tool_inside_an_application_ask_is_a_warning():
    label = off_platform("To apply, send your resume to hiring@gmail.com via Telegram.")
    assert "ask:" in label and "tool:telegram" in label and "email:hiring@gmail.com" in label


def test_an_external_form_link_is_a_warning_but_the_site_itself_is_not():
    assert "url:forms.gle/AbC123" in off_platform("Apply here: forms.gle/AbC123")
    assert off_platform("See more at onlinejobs.ph/job/123") == ""


def test_an_obfuscated_address_is_caught():
    assert "email:bob (at) example (dot) com" in off_platform("Email bob (at) example (dot) com")


def test_the_sites_redaction_leaves_a_shape():
    assert "redacted link" in off_platform("Fill out the form: ----------")


def test_no_description_no_flag():
    assert off_platform(None) == "" and off_platform("") == ""


# ── hours ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("60 hours/week", True), ("40+ hours per week", True), ("over 40 hours", True),
    ("more than 40 hrs", True), ("40 hours/week", False), ("20-30 hours/week", False),
    ("Part time", False), (None, False),
])
def test_over_40_hours(text, want):
    assert over_40_hours(text) is want


# ── stored, not just computable ──────────────────────────────────────────────

def test_enrich_stores_the_flags(conn):
    row_id, _ = job_repo.upsert_stub(conn, job_id=101, job_url="https://x/101",
                                     title="VA", hours="45 hours/week")
    job_repo.enrich_job(conn, row_id, description="To apply: send your CV to a@b.com",
                        hours_per_week="45 hours/week")
    r = conn.execute("SELECT off_platform, over_40h FROM jobs WHERE id = ?", (row_id,)).fetchone()
    assert "email:a@b.com" in r["off_platform"] and r["over_40h"] == 1


def test_the_list_view_hours_set_the_flag_before_enrich(conn):
    row_id, _ = job_repo.upsert_stub(conn, job_id=102, job_url="https://x/102",
                                     title="VA", hours="50 hours/week")
    assert conn.execute("SELECT over_40h FROM jobs WHERE id = ?", (row_id,)).fetchone()[0] == 1


def test_the_older_copy_is_marked_superseded(conn):
    def add(job_id, title):
        return job_repo.upsert_stub(conn, job_id=job_id, job_url=f"https://x/{job_id}",
                                    title=title, company="Acme Inc")[0]
    old_id = add(201, "Executive Assistant")
    new_id = add(202, "Executive Assistant")
    # Repost linking happens when the detail page is read (that is where the
    # employer identity comes from), not from the list-view stub alone.
    job_repo.enrich_job(conn, old_id, description="old")
    job_repo.enrich_job(conn, new_id, description="new")
    old = conn.execute("SELECT repost_of, superseded_by FROM jobs WHERE id = ?", (old_id,)).fetchone()
    new = conn.execute("SELECT repost_of, superseded_by FROM jobs WHERE id = ?", (new_id,)).fetchone()
    assert new["repost_of"] == old_id, "the newer copy points at the original"
    assert old["superseded_by"] == new_id, "and the original says it has been superseded"
    assert new["superseded_by"] is None


def test_migration_backfills_flags_from_text_that_is_already_there(conn):
    """Rows enriched before v14 have the text but not the flags — v14 derives them."""
    row_id, _ = job_repo.upsert_stub(conn, job_id=301, job_url="https://x/301",
                                     title="VA", hours="60 hours/week")
    conn.execute("UPDATE jobs SET description = 'Apply here: forms.gle/xyz', off_platform = '', "
                 "over_40h = 0 WHERE id = ?", (row_id,))
    conn.commit()
    from db.migrations.v14_listing_flags import step
    step(conn)
    r = conn.execute("SELECT off_platform, over_40h FROM jobs WHERE id = ?", (row_id,)).fetchone()
    assert "url:forms.gle/xyz" in r["off_platform"] and r["over_40h"] == 1


def test_api_and_export_carry_the_flags(client):
    from db.connection import get_conn
    conn = get_conn()
    row_id, _ = job_repo.upsert_stub(conn, job_id=401, job_url="https://x/401",
                                     title="VA", hours="55 hours/week")
    job_repo.enrich_job(conn, row_id, description="To apply: DM me on WhatsApp")
    row = next(j for j in client.get("/api/jobs?per_page=100").json()["items"]
               if j["job_id"] == 401)
    assert "tool:whatsapp" in row["off_platform"] and row["over_40h"] == 1
    csv = client.get("/api/jobs/export").text
    assert "off_platform" in csv.splitlines()[0] and "over_40h" in csv.splitlines()[0]
