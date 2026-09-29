"""
V8: the Company column stops holding a login banner (audit L1 + B4).

Logged out, a live OJ.ph detail page has no employer logo — its only <h3> is
"Please login or register as jobseeker to apply for this job." The old
`detail.company` fallback chain ended in a bare `h3`, so that sentence was stored
as the company: 923 of 1,232 rows on the live DB. Consequences: the Company column
was unreadable, the ATS employer field was junk, and repost detection (which needs
employer_id or a usable company) could not fall back at all.

Steps, all idempotent:
1. add jobs.norm_company (+ index) — the key a company-based repost match uses
2. NULL the rows holding the login banner, and drop repost links that rested on it
3. backfill norm_company from whatever company text is real
4. mark reposts the company way, for the ~90% of rows with no employer_id
"""

import logging
import re

log = logging.getLogger(__name__)

# Text that was scraped out of the page chrome, never an employer name.
GARBAGE = (
    "please login or register%",     # logged-out detail page, the whole <h3>
    "login or register%",
)


def norm_company_name(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def step(conn):
    """v8: clean the login-banner company values, add norm_company, backfill reposts."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "norm_company" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN norm_company TEXT")
        cols = cols | {"norm_company"}

    # A pre-W4 DB may not even have a company column; nothing to clean there.
    if "company" not in cols:
        conn.commit()
        return

    n = 0
    for g in GARBAGE:
        n += conn.execute("UPDATE jobs SET company = NULL WHERE lower(company) LIKE ?",
                          (g,)).rowcount
    if n:
        log.info("v8: cleared company on %d rows (scraped page chrome, not an employer)", n)

    # Whatever is left as company text gets its fold; a NULL company has no fold.
    conn.execute(
        "UPDATE jobs SET norm_company = NULL WHERE company IS NULL OR company = ''")
    rows = conn.execute(
        "SELECT id, company FROM jobs "
        "WHERE company IS NOT NULL AND company != '' "
        "AND (norm_company IS NULL OR norm_company = '')"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE jobs SET norm_company = ? WHERE id = ?",
                     (norm_company_name(r[1]), r[0]))
    if rows:
        log.info("v8: norm_company backfilled on %d rows", len(rows))

    # The repost pass needs the columns the key is built from; a pre-W4.3 DB may
    # not have them yet (later migrations add them), so skip rather than crash.
    if not {"norm_title", "employer_id", "repost_of"} <= cols:
        conn.commit()
        return

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_normcompany_normtitle "
        "ON jobs(norm_company, norm_title)"
    )

    # A repost flagged purely by the banner text is not a repost of anything.
    r = conn.execute(
        "UPDATE jobs SET repost_of = NULL "
        "WHERE repost_of IS NOT NULL AND employer_id IS NULL "
        "AND (norm_company IS NULL OR norm_company = '')"
    )
    if r.rowcount:
        log.info("v8: cleared %d repost links that rested on scraped page chrome", r.rowcount)

    # Company-based repost pass: employer_id is NULL on most rows (the logo is
    # behind a jobseeker login), so title + company is what actually identifies
    # a re-post in practice.
    groups: dict[tuple, list[int]] = {}
    for r in conn.execute(
        "SELECT id, employer_id, norm_title, norm_company FROM jobs "
        "WHERE repost_of IS NULL AND norm_title IS NOT NULL AND norm_title != ''"
    ).fetchall():
        key = (r[1] or r[3] or "", r[2])
        if not key[0]:
            continue
        groups.setdefault(key, []).append(r[0])
    marked = 0
    for _key, ids in groups.items():
        if len(ids) < 2:
            continue
        ids.sort()
        for rid in ids[1:]:
            conn.execute("UPDATE jobs SET repost_of = ? WHERE id = ?", (ids[0], rid))
            marked += 1
    if marked:
        log.info("v8: marked %d reposts by title+company", marked)

    conn.commit()