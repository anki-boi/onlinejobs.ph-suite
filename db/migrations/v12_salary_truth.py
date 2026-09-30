"""v12: honest salary columns (the `ojph-cleaner/salary.js` port).

Adds the columns that say HOW a salary was read:

  salary_unit / salary_hours / salary_hours_basis  — per hour, day, month, year, or
      an unrateable piece rate, and which hours figure the month used (stated,
      full-time-assumed, part-time, unstated)
  salary_rate_min / salary_rate_max                — the posted rate converted to
      PHP per unit, which is honest for a day or piece rate where no month is
  salary_assumed_currency                          — 1 when the currency had to be
      guessed from magnitude alone ("42000"), 0 when the listing stated one
  salary_piece_rate                                — 1 when the pay is per item

Then every salary column in the DB is re-derived from the raw `salary` text with
the corrected parser (the boot-time FX refresh re-derives the same columns daily).
FX is fetched live once for the pass; a currency with no live rate keeps its
previous monthly columns instead of an approximation, and the next daily
renormalize() fills them.
"""

import logging

log = logging.getLogger("db.migrate.v12")

NEW_COLS = (
    ("salary_unit", "TEXT"),
    ("salary_hours", "REAL"),
    ("salary_hours_basis", "TEXT"),
    ("salary_rate_min", "REAL"),
    ("salary_rate_max", "REAL"),
    ("salary_assumed_currency", "INTEGER"),
    ("salary_piece_rate", "INTEGER"),
)


def step(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, typ in NEW_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    conn.commit()

    from db.repos.jobs import recompute_salaries
    out = recompute_salaries(conn)
    log.info("v12: re-derived %d salaries (%d with a monthly figure the listing "
             "actually supports)", out["rows"], out["monthly"])
