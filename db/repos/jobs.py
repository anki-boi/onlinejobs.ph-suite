"""
db/repos/jobs.py — All job CRUD and query operations.
"""

import sqlite3
from datetime import datetime

STATUSES = [
    "New", "Interested", "Applied", "Interviewing",
    "Offer", "Hired", "Rejected", "Hidden",
]

SCRAPE_STATUSES = ["Open", "Closed", ""]

# Monotonic version of the jobs table (W2.3): bumped in the same transaction
# as any write that changes a job's score-relevant fields (title, company,
# skills, salary, description). Memoised per-row caches (ATS scores) key on
# this, so in-place enrichment invalidates them — the old cache keyed on
# MAX(id)/COUNT(*) only invalidated when rows were added/deleted.
JOBS_VERSION_KEY = "jobs_version"


def get_jobs_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (JOBS_VERSION_KEY,)
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return 0


def _bump_jobs_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, '1') "
        "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (JOBS_VERSION_KEY,),
    )


def norm_title(title: str | None) -> str:
    """Case/punctuation-insensitive title for repost matching."""
    import re
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


# B4 (audit): the same normalization, named for what it's matching. Employer names
# need the same folding ("Acme, Inc." == "acme inc"), so there is one implementation.
norm_company = norm_title

# L1: the logged-out detail page's only <h3> is page chrome, not an employer. A
# stale parser (or a fixture) must not be able to write it back into the column.
_PAGE_CHROME = ("please login or register", "login or register")


def clean_company(name: str | None) -> str | None:
    """Employer name, or None when the value is scraped page chrome."""
    n = (name or "").strip()
    if not n or any(n.lower().startswith(c) for c in _PAGE_CHROME):
        return None
    return n


SALARY_COLS = ("salary_min", "salary_max", "salary_currency",
               "salary_monthly_min", "salary_monthly_max",
               "salary_unit", "salary_hours", "salary_hours_basis",
               "salary_rate_min", "salary_rate_max",
               "salary_assumed_currency", "salary_piece_rate")
# Columns derivable from the salary text alone. The rest need a live FX rate, and a
# row whose rate is not live keeps what it had rather than being blanked.
SALARY_COLS_NO_FX = ("salary_min", "salary_max", "salary_currency", "salary_unit",
                     "salary_hours", "salary_hours_basis",
                     "salary_assumed_currency", "salary_piece_rate")


def _hours_basis(salary, hours_per_week, work_type, title) -> tuple[float | None, str]:
    """Weekly hours a listing states. Its own HOURS PER WEEK field first (a range
    reads its LOWER bound — understating a month is honest, inflating one is not),
    then an `N hours/week` phrase, then what "full time" means (40 h/week, labelled
    as an assumption). Part-time wins over full-time prose: inventing 40 h for a
    part-timer nearly doubles their month (live: $6/hour → ₱60,223/mo)."""
    import re

    from scraper.salary import hours_per_week_from
    m = re.search(r"\d{1,3}", hours_per_week or "")
    if m and 0 < int(m.group(0)) <= 80:
        return float(m.group(0)), "stated"
    return hours_per_week_from(" ".join(b for b in (salary, work_type, title) if b))


def _columns(s, fx: dict | None, basis: str | None = None) -> dict:
    from scraper.salary import monthly_php, rate_php
    if s is None:
        return {c: None for c in SALARY_COLS}
    pmn, pmx = monthly_php(s, fx)
    rmn, rmx = rate_php(s, fx)
    return {
        "salary_min": s.raw_min, "salary_max": s.raw_max, "salary_currency": s.currency,
        "salary_monthly_min": pmn, "salary_monthly_max": pmx,
        "salary_unit": s.unit, "salary_hours": s.hours,
        # The caller's basis wins: it knows about work_type and the HOURS PER WEEK
        # field, which the salary text alone does not ("$6/hour" + "Part Time").
        "salary_hours_basis": basis or s.hours_basis,
        "salary_rate_min": rmn, "salary_rate_max": rmx,
        "salary_assumed_currency": int(s.assumed_currency),
        "salary_piece_rate": int(s.per_unit),
    }


def salary_columns(salary, *, hours_per_week=None, work_type=None, title=None,
                   fx: dict | None = None) -> dict:
    """Every derived salary column for one salary text.

    `salary_min/max` are the POSTED figures (per hour/day/month, as written).
    `salary_monthly_min/max` are PHP per month — NULL when the listing does not
    support a monthly figure (no stated hours, a day rate, a piece rate): a guessed
    month is a wrong month. FX is live or absent, never stale.
    """
    from scraper.salary import parse_salary
    hours, basis = _hours_basis(salary, hours_per_week, work_type, title)
    return _columns(parse_salary(salary, hours), fx, basis)


def recompute_salaries(conn, fx: dict | None = None) -> dict:
    """Re-derive every salary column from the raw `salary` text — the one source of
    truth for money in this DB. Called by migration v12 and by renormalize() on
    live rates, which runs at boot and daily.

    Rates are fetched once for the whole pass. A currency with no live rate keeps
    its previous monthly columns (never an approximation, never a blanking) and the
    next pass fills them. Returns {"rows", "monthly", "rates"}.
    """
    from scraper.salary import fetch_fx_to_php, parse_salary
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    # A legacy DB may predate the hours/work_type/title columns v12 reads for the
    # hours basis; select only what exists rather than assuming the modern schema.
    extra = [c for c in ("hours_per_week", "work_type", "title") if c in have]
    rows = conn.execute(
        f"SELECT id, salary{', ' + ', '.join(extra) if extra else ''} FROM jobs "
        "WHERE salary IS NOT NULL AND salary != ''").fetchall()
    parsed = []
    curs: set[str] = set()
    for r in rows:
        keys = r.keys()
        hours, _b = _hours_basis(r["salary"],
                                 r["hours_per_week"] if "hours_per_week" in keys else None,
                                 r["work_type"] if "work_type" in keys else None,
                                 r["title"] if "title" in keys else None)
        s = parse_salary(r["salary"], hours)
        parsed.append((r["id"], s))
        if s and s.currency != "PHP":
            curs.add(s.currency)
    if fx is None:
        fx = fetch_fx_to_php(sorted(curs)) if curs else {}
    fx = {str(k).upper(): v for k, v in fx.items()}
    monthly = 0
    for row_id, s in parsed:
        cols = _columns(s, fx, _b)
        no_rate = s is not None and s.currency != "PHP" and s.currency not in fx
        cols_to_write = SALARY_COLS_NO_FX if no_rate else SALARY_COLS
        monthly += cols["salary_monthly_min"] is not None
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in cols_to_write)} WHERE id = ?",
            [cols[c] for c in cols_to_write] + [row_id])
    conn.commit()
    _record_fx_metadata(conn, fx)
    return {"rows": len(parsed), "monthly": monthly, "rates": fx}


def _record_fx_metadata(conn, fx: dict) -> None:
    """Rates + timestamp stored together, so the UI can state what it normalized at."""
    if not fx:
        return
    import json
    import time as _time

    from db.repos import settings as settings_repo
    raw = settings_repo.get(conn, "fx_metadata", "")
    meta = json.loads(raw) if raw else {}
    meta.update({k.lower(): v for k, v in fx.items()})
    meta["at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
    settings_repo.set(conn, "fx_metadata", json.dumps(meta))


def renormalize(conn) -> dict:
    """Recompute every salary column from LIVE rates (server start + daily).

    Fills rows that were NULL because no live rate was available, and fixes rows
    whose stored figure is now outdated (a stale ₱ number is a wrong number).
    Returns the live rate table used (possibly {}).
    """
    return recompute_salaries(conn)["rates"]


def _find_repost_origin(conn, row_id: int, title: str, employer_id: int | None = None,
                        company: str | None = None) -> int | None:
    """Earliest non-repost row with the same normalized title, matched on the
    strongest key available: employer_id when we have it, otherwise the company
    name (B4: employer_id is NULL on 1,115 of 1,232 rows — the employer logo is
    behind a jobseeker login — so the company text is the only key that exists
    for most of the DB, and reposts went largely undetected).

    Index-backed on (norm_title, employer_id) / (norm_company, norm_title); the
    old version re-queried the title of every candidate row one at a time."""
    nt = norm_title(title)
    if not nt:
        return None
    if employer_id:
        cond, arg = "employer_id = ?", employer_id
    else:
        nc = norm_company(company)
        if not nc:
            return None
        cond, arg = "norm_company = ?", nc
    row = conn.execute(
        f"SELECT id FROM jobs WHERE norm_title = ? AND id != ? "
        f"AND repost_of IS NULL AND {cond} ORDER BY id LIMIT 1",
        (nt, row_id, arg),
    ).fetchone()
    return row["id"] if row else None


# Columns the API may sort by (Excel-style header sorting).
# B14: `skills` dropped — sorting by a comma-joined tag string is meaningless.
SORTABLE = {
    "title", "company", "salary", "location", "hours_per_week", "work_type",
    "posted_date", "date_updated", "date_found", "status", "scrape_status", "ats",
}

# B14: the order a job actually moves through, used when sorting by status.
STATUS_ORDER = ["New", "Interested", "Applied", "Interviewing", "Offer",
                "Hired", "Rejected", "Hidden"]

def _ats_columns(conn: sqlite3.Connection) -> set:
    """Columns the materialized score cache actually has.

    A pre-W4.3 database has no ats_scores at all and a pre-v9 one has no `fit`,
    and get_jobs runs against both (migrations add them on boot, tests build
    legacy shapes directly). Asking once per query keeps the SELECT honest
    instead of throwing `no such column: fit` at a half-migrated DB."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ats_scores'"
    ).fetchone():
        return set()
    return {r[1] for r in conn.execute("PRAGMA table_info(ats_scores)")}


def _split_multi(value: str | None) -> list[str]:
    """'a,b, c' → ['a','b','c'] (stripped, non-empty)."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def get_jobs(
    conn: sqlite3.Connection,
    page: int = 1,
    per_page: int = 50,
    status: str | None = None,
    search: str | None = None,
    include_hidden: bool = False,
    work_type: str | None = None,
    skill: str | None = None,
    skills: str | None = None,
    categories: str | None = None,
    scrape_status: str | None = None,
    sort: str | None = None,
    order: str = "desc",
    title: str | None = None,
    company: str | None = None,
    salary: str | None = None,
    location: str | None = None,
    hours: str | None = None,
    posted_from: str | None = None,
    posted_to: str | None = None,
    has_salary: bool = False,
    min_ats: int = 0,
    min_fit: int = 0,
    hide_reposts: bool = False,
    include_deleted: bool = False,
    salary_min_monthly: float | None = None,
    salary_max_monthly: float | None = None,
    salary_currency: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Return (rows, total_count) with optional filters and pagination.

    Multi-value filters (status, work_type, scrape_status, skills) take
    comma-separated strings and OR within their own group.
    Text filters (title, company, salary, location, hours) are LIKE matches.
    `posted_from` / `posted_to` (inclusive date range) filter on the *displayed*
    posted date: posted_date, falling back to date_found when the posted date
    was never captured.
    `sort` must be in SORTABLE, else the default (newest first) applies.
    """
    clauses: list[str] = []
    params: list = []
    acols = _ats_columns(conn)

    if not include_hidden:
        clauses.append("status != 'Hidden'")

    # P6: a soft-deleted row is invisible everywhere unless you ask for it.
    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    if status:
        statuses = _split_multi(status)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

    if work_type:
        wts = _split_multi(work_type)
        if wts:
            placeholders = ",".join("?" * len(wts))
            clauses.append(f"work_type IN ({placeholders})")
            params.extend(wts)

    if scrape_status:
        ss = _split_multi(scrape_status)
        if ss:
            placeholders = ",".join("?" * len(ss))
            clauses.append(f"scrape_status IN ({placeholders})")
            params.extend(ss)

    if skills:
        sks = _split_multi(skills)
        if sks:
            clauses.append("(" + " OR ".join("skills LIKE ?" for _ in sks) + ")")
            params.extend(f"%{s}%" for s in sks)

    # F5 (audit): the category a job hangs under, harvested from its tag href
    # (L4). The browser used to fake this by substring-matching the category name
    # against the rendered title/company text.
    if categories:
        cats = _split_multi(categories)
        if cats:
            clauses.append("(" + " OR ".join("search_category = ?" for _ in cats) + ")")
            params.extend(cats)

    if skill:
        clauses.append("skills LIKE ?")
        params.append(f"%{skill}%")

    for col, val in (("title", title), ("company", company), ("salary", salary),
                     ("location", location), ("hours_per_week", hours)):
        if val:
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{val}%")

    if posted_from or posted_to:
        # Same expression the table cell displays (posted_date or date_found fallback),
        # so the filter matches what the user sees. date() makes both forms
        # ("YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS") compare as calendar dates.
        eff = "date(COALESCE(NULLIF(posted_date, ''), date_found))"
        if posted_from:
            clauses.append(f"{eff} >= ?")
            params.append(posted_from)
        if posted_to:
            clauses.append(f"{eff} <= ?")
            params.append(posted_to)

    if has_salary:
        # "Has a salary" = the field contains at least one digit. Excludes every
        # no-salary label (TBD, N/A, Negotiable, DOE, "to be discussed", empty, NULL)
        # regardless of wording. Verified against the live DB: 2,027 in / 283 out.
        clauses.append("salary GLOB '*[0-9]*'")

    # W4.2: money filters on the PHP-normalized columns (W4.1), so a mixed
    # USD/PHP result set is comparable. Rows without a normalized salary
    # (TBD/DOE) never match a numeric bound.
    if salary_min_monthly:
        clauses.append("salary_monthly_max >= ?")
        params.append(salary_min_monthly)
    if salary_max_monthly is not None:
        clauses.append("COALESCE(salary_monthly_min, salary_monthly_max) <= ?")
        params.append(salary_max_monthly)
    if salary_currency:
        curs = _split_multi(salary_currency)
        if curs:
            placeholders = ",".join("?" * len(curs))
            clauses.append(f"salary_currency IN ({placeholders})")
            params.extend(curs)

    # W4.3: best-profile ATS at SQL level (materialized ats_scores, W4.3) so
    # `total` is a truthful global count, not a per-page Python filter.
    if min_ats and "total" in acols:
        clauses.append(
            "EXISTS (SELECT 1 FROM ats_scores a "
            "WHERE a.job_id = jobs.id AND a.total >= ?)"
        )
        params.append(min_ats)

    # P1 (audit): the job-worthiness floor is `fit` — skills + keywords out of 60.
    # `total` adds 40 points of resume hygiene that are identical for every row,
    # so "ATS >= 50" was really asking "is my resume well-formed?" and hid nearly
    # the whole table.
    if min_fit and "fit" in acols:
        clauses.append(
            "EXISTS (SELECT 1 FROM ats_scores a "
            "WHERE a.job_id = jobs.id AND a.fit >= ?)"
        )
        params.append(min_fit)

    # F2/B11: "Hide reposts" was a client-side filter over the current page only,
    # so the CSV and the count disagreed with the screen. Now it is a real filter.
    if hide_reposts:
        clauses.append("repost_of IS NULL")

    if search:
        like = f"%{search}%"
        clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ? OR search_keyword LIKE ?)")
        params.extend([like, like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM jobs {where}", params
    ).fetchone()[0]

    if sort in SORTABLE:
        order_sql = "ASC" if order.strip().lower() == "asc" else "DESC"
        if sort == "posted_date":
            # Sort on the displayed value (posted_date, falling back to date_found
            # when the posted date is missing). SQLite would otherwise put
            # NULL-posted_date rows at the top of ASC while their cell shows a
            # recent date_found — which reads as a broken sort.
            order_by = f"date(COALESCE(NULLIF(posted_date, ''), date_found)) {order_sql}"
        elif sort == "salary":
            # W4.1: numeric, PHP-normalized. The IS-NULL flag sorts first, so
            # no-salary rows (TBD/DOE/NULL) sink to the bottom in both
            # directions instead of scattering by lexicographic order.
            order_by = (
                f"(salary_monthly_max IS NULL) ASC, salary_monthly_max {order_sql}, "
                f"id ASC"
            )  # id tie-break: equal salaries order deterministically
        elif sort == "hours_per_week":
            # B14: the column is free text ("20-30 hours/week"), so "4" sorted
            # above "40". Order on the leading number; unparsable text last.
            order_by = (
                "CASE WHEN hours_per_week GLOB '[0-9]*' THEN "
                "CAST(CASE WHEN instr(hours_per_week,'-') > 0 "
                "THEN substr(hours_per_week, 1, instr(hours_per_week,'-') - 1) "
                "ELSE hours_per_week END AS INTEGER) ELSE 1e9 END " + order_sql
            )
        elif sort == "status":
            # B14: workflow order, not alphabetical — a-z made the column useless.
            rank = "CASE status " + " ".join(
                f"WHEN '{s}' THEN {i}" for i, s in enumerate(STATUS_ORDER)
            ) + " ELSE 99 END"
            order_by = f"{rank} {order_sql}"
        elif sort == "ats" and "fit" in acols:
            # P1: sort on fit, the job-dependent half. Unscored jobs sink (-1),
            # in both directions — SQLite parks a bare NULL at the top of a DESC.
            order_by = ("COALESCE((SELECT fit FROM ats_scores a "
                        "WHERE a.job_id = jobs.id ORDER BY total DESC LIMIT 1), -1) "
                        + order_sql)
        else:
            order_by = f"{sort} {order_sql}"
    else:
        order_by = "date_found DESC"

    offset = (page - 1) * per_page
    limit_sql = "" if per_page <= 0 else "LIMIT ? OFFSET ?"
    tail_params = [] if per_page <= 0 else [per_page, offset]
    # The best-profile score travels with each row: the table showed no ATS at all
    # while filtering on it, which is P1's other half — a filter the user can't see
    # the input for. Correlated subqueries keep one row per job even if an old
    # cache left several profiles behind.
    def _score(col, alias):
        if col not in acols:
            return f"NULL AS {alias}"
        return (f"(SELECT {col} FROM ats_scores a WHERE a.job_id = jobs.id "
                f"ORDER BY a.total DESC LIMIT 1) AS {alias}")

    rows = conn.execute(
        f"SELECT jobs.*, "
        f"{_score('total', 'ats_total')}, "
        f"{_score('fit', 'ats_fit')}, "
        f"{_score('profile', 'ats_profile')} "
        f"FROM jobs {where} ORDER BY {order_by}, id DESC {limit_sql}",
        params + tail_params,
    ).fetchall()

    return rows, total


def get_job(conn: sqlite3.Connection, job_pk: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_pk,)).fetchone()


def get_job_by_ojid(conn: sqlite3.Connection, oj_job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (oj_job_id,)).fetchone()


def upsert_stub(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    job_url: str,
    title: str | None = None,
    work_type: str | None = None,
    company: str | None = None,
    posted_date: str | None = None,
    salary: str | None = None,
    location: str | None = None,
    hours: str | None = None,
    skills: list[str] | None = None,
    search_keyword: str | None = None,
    search_category: str | None = None,
) -> tuple[int, bool]:
    """
    Insert a new job stub from the search list view.
    Returns (row_id, is_new). If the job already exists (by job_id or job_url),
    update the list-view fields and return the existing ID.
    """
    skills_str = ", ".join(skills) if skills else None
    company = clean_company(company)
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sal = (salary_columns(salary, hours_per_week=hours, work_type=work_type, title=title)
           if salary is not None else {c: None for c in SALARY_COLS})

    # Check by job_id first, then job_url
    existing = None
    if job_id:
        existing = conn.execute(
            "SELECT id, salary FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if not existing:
        existing = conn.execute(
            "SELECT id, salary FROM jobs WHERE job_url = ?", (job_url,)
        ).fetchone()

    if existing:
        row_id = existing["id"]
        # Only update fields that are not None (don't clobber existing data)
        updates = []
        vals = []
        for col, val in [
            ("title", title), ("work_type", work_type), ("company", company),
            ("posted_date", posted_date), ("salary", salary), ("location", location),
            ("hours_per_week", hours),
        ]:
            if val is not None:
                updates.append(f"{col} = ?")
                vals.append(val)
        if title is not None and norm_title(title):
            updates.append("norm_title = ?")
            vals.append(norm_title(title))
        if company is not None:
            updates.append("norm_company = ?")
            vals.append(norm_company(company))
        if salary is not None:
            # Only touch the structured fields when we actually got a value,
            # or when the salary text genuinely changed ("$800/mo" → "TBD"
            # invalidates the captured numbers). A parse failure on the
            # same text must not clobber previously captured figures.
            if sal["salary_min"] is not None or salary != existing["salary"]:
                updates += [f"{c} = ?" for c in SALARY_COLS]
                vals += [sal[c] for c in SALARY_COLS]
        if skills_str:
            updates.append("skills = ?")
            vals.append(skills_str)
        if search_keyword:
            updates.append("search_keyword = ?")
            vals.append(search_keyword)
        if search_category:
            updates.append("search_category = ?")
            vals.append(search_category)
        if updates:
            vals.append(row_id)
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", vals)
            _bump_jobs_version(conn)
        conn.commit()
        return row_id, False

    conn.execute(
        """INSERT INTO jobs
           (job_id, job_url, title, work_type, company, posted_date,
            salary, location, hours_per_week, skills, search_keyword,
            search_category, date_found, status, norm_title,
            salary_min, salary_max, salary_currency,
            salary_monthly_min, salary_monthly_max,
            salary_unit, salary_hours, salary_hours_basis,
            salary_rate_min, salary_rate_max,
            salary_assumed_currency, salary_piece_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'New', ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, job_url, title, work_type, company, posted_date,
            salary, location, hours, skills_str, search_keyword,
            search_category, date_now,
            norm_title(title) if title else None,
            *[sal[c] for c in SALARY_COLS],
        ),
    )
    conn.execute("UPDATE jobs SET norm_company = ? WHERE id = last_insert_rowid()",
                 (norm_company(company),))
    # B3 (audit): the INSERT path never bumped jobs_version, so a freshly
    # harvested job had no ats_scores row until some unrelated UPDATE happened
    # to bump it — and under "ATS >= 50" (an EXISTS on ats_scores) those brand-new
    # jobs were invisible. Observed live: 1,232 jobs vs 1,143 score rows.
    _bump_jobs_version(conn)
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"], True


def enrich_job(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    title: str | None = None,
    company: str | None = None,
    description: str | None = None,
    salary: str | None = None,
    hours_per_week: str | None = None,
    work_type: str | None = None,
    date_updated: str | None = None,
    skills: list[str] | None = None,
    category: str | None = None,
    employer_id: int | None = None,
    is_closed: bool = False,
    close_reason: str | None = None,
) -> None:
    """Update a job with detail-page data. Only overwrites non-None values."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scrape_status = "Closed" if is_closed else "Open"
    company = clean_company(company)

    fields = {
        "scrape_status": scrape_status,
        "scrape_reason": close_reason or "",
        "last_checked": now,
    }
    for col, val in [
        ("title", title), ("company", company), ("description", description),
        ("salary", salary), ("hours_per_week", hours_per_week),
        ("work_type", work_type), ("date_updated", date_updated),
        ("employer_id", employer_id),
    ]:
        if val is not None:
            fields[col] = val
    if skills is not None:
        fields["skills"] = ", ".join(skills)
    if category:
        # L4: a re-check backfills search_category on rows harvested before the
        # parser knew how to read it (it was NULL on all 1,232 of them).
        fields["search_category"] = category
    if title is not None and norm_title(title):
        fields["norm_title"] = norm_title(title)
    if company is not None:
        fields["norm_company"] = norm_company(company)

    # Structured salary (raw min/max + currency + PHP-normalized monthly)
    # follows the salary text
    if salary is not None:
        cols = salary_columns(salary, hours_per_week=hours_per_week, work_type=work_type,
                              title=title)
        old = conn.execute("SELECT salary FROM jobs WHERE id = ?", (row_id,)).fetchone()
        old_text = old["salary"] if old is not None else None
        # Same clobber guard as upsert_stub: a parse failure on unchanged
        # text must not erase previously captured figures
        if cols["salary_min"] is not None or salary != old_text:
            for c in SALARY_COLS:
                fields[c] = cols[c]

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [row_id]
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", vals)
    if any(v is not None for v in (title, company, description, salary, skills)):
        _bump_jobs_version(conn)

    # Repost detection needs the effective (possibly pre-existing) title + employer
    row = conn.execute("SELECT title, employer_id, company FROM jobs WHERE id=?", (row_id,)).fetchone()
    if row and row["title"]:
        origin = _find_repost_origin(conn, row_id, row["title"],
                                     row["employer_id"], row["company"])
        conn.execute("UPDATE jobs SET repost_of=? WHERE id=?", (origin, row_id))

    conn.commit()


def update_status(conn: sqlite3.Connection, row_id: int, new_status: str) -> None:
    """Update workflow status + write to history.

    A manual status change always takes over from the keyword filter:
    the filter_hidden flag is cleared so the filter will never clobber
    a status the user explicitly set.
    """
    if new_status not in STATUSES:
        raise ValueError(f"Invalid status: {new_status}")
    row = get_job(conn, row_id)
    if not row:
        raise ValueError(f"Job {row_id} not found")
    old_status = row["status"]
    conn.execute(
        "UPDATE jobs SET status = ?, filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
        (new_status, row_id),
    )
    conn.execute(
        "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, ?, ?)",
        (row_id, old_status, new_status),
    )
    conn.commit()


def update_notes(conn: sqlite3.Connection, row_id: int, notes: str) -> None:
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, row_id))
    conn.commit()


def update_follow_up(conn: sqlite3.Connection, row_id: int, follow_up: str) -> None:
    conn.execute("UPDATE jobs SET follow_up = ? WHERE id = ?", (follow_up, row_id))
    conn.commit()


def get_existing_job_ids(conn: sqlite3.Connection) -> set[int]:
    """Return set of all known job_ids (for dedup during harvest)."""
    rows = conn.execute("SELECT job_id FROM jobs WHERE job_id IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def get_existing_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT job_url FROM jobs").fetchall()
    return {r[0] for r in rows}


def get_jobs_needing_enrichment(
    conn: sqlite3.Connection,
    max_age_days: int = 7,
    status_filter: list[str] | None = None,
    base_url: str | None = None,
) -> list[sqlite3.Row]:
    """Jobs that need detail-page re-check: never checked, or checked > max_age_days ago.

    When `base_url` is given, only jobs whose URL is under that site are returned —
    stray rows (e.g. test fixtures pointing at fake domains) would otherwise burn
    retries and produce an error on every check run.
    """
    clauses = [
        "(last_checked IS NULL OR last_checked = '' OR last_checked < datetime('now', ?))"
    ]
    params: list = [f"-{max_age_days} days"]

    if base_url:
        clauses.append("job_url LIKE ?")
        params.append(base_url.rstrip('/') + '/%')

    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        clauses.append(f"status IN ({placeholders})")
        params.extend(status_filter)

    where = f"WHERE {' AND '.join(clauses)}"
    return conn.execute(
        f"SELECT id, job_url, status FROM jobs {where} ORDER BY id", params
    ).fetchall()


def get_stats(conn: sqlite3.Connection) -> dict:
    """Status counts + scrape_status counts + totals."""
    status_rows = conn.execute(
        "SELECT status, COUNT(*) as c FROM jobs WHERE deleted_at IS NULL GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["c"] for r in status_rows}
    stats["total"] = sum(stats.values())

    scrape_rows = conn.execute(
        "SELECT scrape_status, COUNT(*) as c FROM jobs "
        "WHERE scrape_status != '' AND deleted_at IS NULL GROUP BY scrape_status"
    ).fetchall()
    stats["scrape"] = {r["scrape_status"]: r["c"] for r in scrape_rows}

    stats["filter_hidden"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE filter_hidden = 1 AND deleted_at IS NULL"
    ).fetchone()[0]

    # Active-pipeline jobs whose follow-up date is today or in the past.
    # P7 (audit): the old allow-list forgot 'New' and 'Offer', so a job you sat on
    # since it landed, or one you're waiting on an offer for, never counted.
    stats["follow_ups_due"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE follow_up IS NOT NULL AND follow_up != '' "
        "AND date(follow_up) <= date('now') "
        "AND status NOT IN ('Hidden', 'Rejected', 'Hired') AND deleted_at IS NULL"
    ).fetchone()[0]

    return stats


def get_job_history(conn: sqlite3.Connection, job_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM job_history WHERE job_id = ? ORDER BY id DESC",
        (job_pk,),
    ).fetchall()
