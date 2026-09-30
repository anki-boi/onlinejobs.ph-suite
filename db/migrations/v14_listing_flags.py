"""v14: listing flags — off-platform ask, over-40-hour week, superseded duplicate.

Three things the dashboard knew but never showed:

- `off_platform` — the detail text asks for WhatsApp / a Google Form / an email
  address, i.e. the application leaves the platform you are tracking. Measured on
  live listings by ojph-cleaner: a tool name only counts inside a sentence that
  also asks you to apply, so "monitor Telegram accounts" is not a warning.
- `over_40h` — the listing asks for more than a 40-hour week.
- `superseded_by` — an identical newer repost exists, so this (older) row is the
  stale copy. Previously only the newer row was marked, and the stale one — where
  your status and history live — looked current.

All three are re-derived from text the DB already holds, so a re-run is safe.
"""

import logging

from scraper.offplatform import off_platform, over_40_hours

log = logging.getLogger("db.migrate.v14")


def step(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, typ in (("off_platform", "TEXT DEFAULT ''"),
                     ("over_40h", "INTEGER NOT NULL DEFAULT 0"),
                     ("superseded_by", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    conn.commit()

    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    want = [c for c in ("id", "description", "hours_per_week", "repost_of") if c in have]
    flagged = 0
    for r in conn.execute(f"SELECT {', '.join(want)} FROM jobs").fetchall():
        keys = r.keys()
        label = off_platform(r["description"]) if "description" in keys else ""
        over = int(over_40_hours(r["hours_per_week"])) if "hours_per_week" in keys else 0
        conn.execute("UPDATE jobs SET off_platform = ?, over_40h = ? WHERE id = ?",
                     (label, over, r["id"]))
        if label:
            flagged += 1

    # The newer copy points at the origin (repost_of); mark the origin as superseded.
    superseded = 0
    for r in conn.execute("SELECT id, repost_of FROM jobs WHERE repost_of IS NOT NULL").fetchall():
        conn.execute("UPDATE jobs SET superseded_by = ? WHERE id = ?", (r["id"], r["repost_of"]))
        superseded += 1
    conn.commit()
    log.info("v14: %d listings ask off-platform, %d want >40 h/week, %d older copies "
             "marked superseded", flagged,
             conn.execute("SELECT COUNT(*) FROM jobs WHERE over_40h = 1").fetchone()[0],
             superseded)
