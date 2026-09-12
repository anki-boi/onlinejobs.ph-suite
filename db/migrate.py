"""
db/migrate.py — Versioned, idempotent migrations for older database files.

`MIGRATIONS` maps schema version → step function. `run(conn, target)` applies
steps current+1 .. target in order, bumping `PRAGMA user_version` after each
one succeeds. A step that raises leaves the version at the last completed
step, so the next boot retries it — every step is therefore idempotent
(IF NOT EXISTS / column guards / NULL-filtered UPDATEs, no-ops on new DBs).

A fresh DB file starts at version 0 and runs every step — each step is a
no-op on a schema that already has its final shape.

CLI:  python -m db.migrate --dry-run [--db PATH]
"""

import logging

log = logging.getLogger(__name__)

# Old column → new column mappings (v1, pre-2.0 schema)
_COLUMN_RENAMES = [
    ("job_link", "job_url"),
    ("job_title", "title"),
    ("search_tag", "search_keyword"),
    ("tags_found", "skills"),
]


# ── Versioned steps ──────────────────────────────────────────────────────────


def _v1(conn) -> None:
    """v1: legacy schema → current columns (job_link→job_url renames, add
    missing columns, populate job_id, normalise old status values)."""
    existing = _existing_columns(conn, "jobs")

    new_cols = {
        "job_id":          "INTEGER",
        "job_url":         "TEXT",
        "title":           "TEXT",
        "location":        "TEXT",
        "hours_per_week":  "TEXT",
        "work_type":       "TEXT",
        "posted_date":     "TEXT",
        "date_updated":    "TEXT",
        "skills":          "TEXT",
        "employer_id":     "INTEGER",
        "search_keyword":  "TEXT",
        "search_category": "TEXT",
        "scrape_status":   "TEXT DEFAULT ''",
        "scrape_reason":   "TEXT DEFAULT ''",
        "last_checked":    "TEXT",
        "filter_hidden":   "INTEGER NOT NULL DEFAULT 0",
        "pre_filter_status": "TEXT DEFAULT ''",
    }
    for col, col_type in new_cols.items():
        if col not in existing:
            log.info("v1: adding column jobs.%s", col)
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
    existing.update(new_cols)  # just added — the rename pass below must see them

    for old_col, new_col in _COLUMN_RENAMES:
        if old_col in existing and new_col in existing:
            row = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {new_col} IS NULL OR {new_col} = ''"
            ).fetchone()
            if row[0] > 0:
                conn.execute(
                    f"UPDATE jobs SET {new_col} = {old_col} "
                    f"WHERE ({new_col} IS NULL OR {new_col} = '') AND {old_col} IS NOT NULL AND {old_col} != ''"
                )
                log.info("v1: migrated %s → %s", old_col, new_col)

    _populate_job_ids(conn)

    # Old code wrote "Open"/"Closed" into the workflow status column.
    conn.execute("UPDATE jobs SET status = 'New'  WHERE status = 'Open'")
    conn.execute("UPDATE jobs SET status = 'Hidden' WHERE status = 'Closed'")
    conn.execute("UPDATE jobs SET status = 'New'  WHERE status IS NULL OR status = ''")


def _v2(conn) -> None:
    """v2: seed scrape_status + one-time repair — un-gated pre-2.0 code
    stamped 'Open' onto jobs that were never checked; a never-enriched job's
    state is 'unknown' (empty), not 'Open'."""
    conn.execute(
        "UPDATE jobs SET scrape_status = 'Open' "
        "WHERE scrape_status = '' AND status = 'New' AND job_url IS NOT NULL"
    )
    conn.execute(
        "UPDATE jobs SET scrape_status = '', scrape_reason = '' "
        "WHERE (last_checked IS NULL OR last_checked = '') AND scrape_status IN ('Open', 'Closed')"
    )


def _v3(conn) -> None:
    """v3: key/value table for auto-run state (new DBs already have it via
    SCHEMA)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)"
    )


def _v4(conn) -> None:
    """v4: structured salary columns + repost detection, backfilled from
    legacy text."""
    existing = _existing_columns(conn, "jobs")
    for col, col_type in {
        "repost_of":  "INTEGER",
        "salary_min": "REAL",
        "salary_max": "REAL",
    }.items():
        if col not in existing:
            log.info("v4: adding column jobs.%s", col)
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
    _backfill_salary(conn)
    _backfill_reposts(conn)


# version → step (kept in one place; init_db applies up to its target)
MIGRATIONS: dict[int, callable] = {
    1: _v1,
    2: _v2,
    3: _v3,
    4: _v4,
}


def run(conn, target: int) -> None:
    """Apply unapplied migration steps up to `target`, bumping user_version
    after each. A raising step leaves the version unchanged (retried next
    boot) — safe because every step is idempotent."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in range(current + 1, target + 1):
        step = MIGRATIONS.get(v)
        if step is None:
            log.warning(f"migration: no step defined for v{v} — bumping version only")
        else:
            desc = (step.__doc__ or "").strip().splitlines()[0] if step.__doc__ else ""
            log.info(f"migration: applying v{v} — {desc}")
            step(conn)
        conn.execute(f"PRAGMA user_version = {v}")
        conn.commit()


def steps_to_apply(conn, target: int) -> list[int]:
    """Versions that `run()` would apply, for --dry-run and tests."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    return [v for v in range(current + 1, target + 1) if v in MIGRATIONS]


# ── helpers ──────────────────────────────────────────────────────────────────


def _backfill_salary(conn) -> None:
    """Parse salary strings already in the DB into salary_min/salary_max."""
    from scraper.salary import parse_salary
    rows = conn.execute(
        "SELECT id, salary FROM jobs WHERE (salary IS NOT NULL AND salary != '') "
        "AND salary_min IS NULL"
    ).fetchall()
    n = 0
    for r in rows:
        mn, mx, _cur = parse_salary(r["salary"])
        if mn is not None:
            conn.execute("UPDATE jobs SET salary_min=?, salary_max=? WHERE id=?", (mn, mx, r["id"]))
            n += 1
    if n:
        log.info("Backfilled salary ranges for %d jobs", n)
        conn.commit()


def _backfill_reposts(conn) -> None:
    """Mark duplicate listings: same normalized title + same employer.
    The earliest listing is the original; later ones get repost_of set."""
    from db.repos.jobs import norm_title
    rows = conn.execute(
        "SELECT id, title, employer_id FROM jobs "
        "WHERE title IS NOT NULL AND employer_id IS NOT NULL AND repost_of IS NULL"
    ).fetchall()
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        groups.setdefault((r["employer_id"], norm_title(r["title"])), []).append(r["id"])
    n = 0
    for _key, ids in groups.items():
        if len(ids) < 2:
            continue
        ids.sort()  # insertion order ~ discovery order; first is the original
        for rid in ids[1:]:
            conn.execute("UPDATE jobs SET repost_of=? WHERE id=?", (ids[0], rid))
            n += 1
    if n:
        log.info("Marked %d reposts", n)
        conn.commit()


def _existing_columns(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _populate_job_ids(conn) -> None:
    """Extract numeric job ID from the URL slug for rows that don't have one yet."""
    import re
    rows = conn.execute(
        "SELECT id, job_url FROM jobs WHERE job_id IS NULL AND job_url IS NOT NULL"
    ).fetchall()
    for row in rows:
        url = row["job_url"]
        match = re.search(r"/job/[^/]+?-(\d+)$", url)
        if match:
            conn.execute(
                "UPDATE jobs SET job_id = ? WHERE id = ? AND job_id IS NULL",
                (int(match.group(1)), row["id"]),
            )
    if rows:
        log.info("Populated job_id for %d rows", len(rows))


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    import db.connection as dbconn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="list the migrations that would run, without applying them")
    parser.add_argument("--db", default=None, help="DB file (default: the configured one)")
    args = parser.parse_args(argv)

    # ponytail: CLI prints ASCII only - Windows cp1252 consoles mangle dashes.
    path = dbconn.get_db_path(args.db)
    if not path.exists():
        print(f"no such DB file: {path}")
        return 1
    conn = dbconn.get_conn(str(path))  # 30s busy timeout, Row factory

    steps = steps_to_apply(conn, dbconn.SCHEMA_VERSION)
    if args.dry_run:
        print(f"db: {path}")
        print(f"current version: {conn.execute('PRAGMA user_version').fetchone()[0]}, "
              f"target: {dbconn.SCHEMA_VERSION}")
        if not steps:
            print("up to date - nothing to do")
        for v in steps:
            doc = MIGRATIONS[v].__doc__ or ""
            print(f"  would apply v{v}: {doc.strip().splitlines()[0]}")
        conn.close()
        return 0

    if steps:
        run(conn, dbconn.SCHEMA_VERSION)
        print(f"migrated to v{conn.execute('PRAGMA user_version').fetchone()[0]}")
    else:
        print(f"up to date (v{conn.execute('PRAGMA user_version').fetchone()[0]})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

