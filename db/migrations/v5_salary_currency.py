"""v5: currency-aware salary (spec W4.1 / §4.3).

Adds `jobs.salary_currency` + PHP-normalized `salary_monthly_min/max`
(FX rate from config `fx_to_php`, default USD 58.0), plus `norm_title` and
`deleted_at` (the §4.3 v5 columns W4.5/W4.6 build on). Backfills existing
salary text, keeps the raw string and the v4 raw min/max, and records the
rate used in `app_settings.fx_metadata` (quality bar: rate + timestamp
stored together so the UI can say what rate it used).

Idempotent: column guards, backfill only touches not-yet-normalized rows,
and the version gate in db/migrate.run() prevents normal re-runs.
"""

import json
import logging

log = logging.getLogger(__name__)

FX_METADATA_KEY = "fx_metadata"


def step(conn) -> None:
    """v5: currency-aware salary (PHP-normalized monthly + FX metadata),
    norm_title/deleted_at columns; backfills salary text, records the rate used."""
    from scraper.salary import DEFAULT_FX_TO_PHP, normalize_to_php, parse_salary

    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, col_type in (
        ("salary_currency", "TEXT"),
        ("salary_monthly_min", "REAL"),
        ("salary_monthly_max", "REAL"),
        ("norm_title", "TEXT"),
        ("deleted_at", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")

    # Read config straight from disk (not the live cache): a migration runs
    # inside init_db and must not poison the live state with a pre-reload file.
    from app import config as appconfig
    cfg = appconfig._read_disk()
    fx = cfg.get("fx_to_php") or {}

    rows = conn.execute(
        "SELECT id, salary FROM jobs "
        "WHERE salary IS NOT NULL AND salary != '' "
        "AND salary_currency IS NULL AND salary_monthly_min IS NULL"
    ).fetchall()
    n = 0
    for r in rows:
        mn, mx, cur = parse_salary(r["salary"])
        pmn, pmx = normalize_to_php(mn, mx, cur, fx)
        if pmn is not None:
            conn.execute(
                "UPDATE jobs SET salary_currency = ?, salary_monthly_min = ?, "
                "salary_monthly_max = ? WHERE id = ?",
                (cur, pmn, pmx, r["id"]),
            )
            n += 1
    if n:
        meta = {"usd": DEFAULT_FX_TO_PHP["USD"]}
        meta.update({str(k).lower(): v for k, v in fx.items()})
        meta["at"] = _now()
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (FX_METADATA_KEY, json.dumps(meta)),
        )
        log.info("v5: normalized %d salaries to PHP", n)

    # norm_title backfill (same shape as the v4 repost backfill)
    from db.repos.jobs import norm_title
    rows = conn.execute(
        "SELECT id, title FROM jobs WHERE title IS NOT NULL AND norm_title IS NULL"
    ).fetchall()
    for r in rows:
        nt = norm_title(r["title"])
        if nt:
            conn.execute("UPDATE jobs SET norm_title = ? WHERE id = ?", (nt, r["id"]))
    conn.commit()


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
