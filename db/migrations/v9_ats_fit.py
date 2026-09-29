"""
V9: ats_scores gains the `fit` half of the score (audit P1).

`total` mixed two different things: how well the resume fits THIS job (skills 40 +
keywords 20) and how well-formed the resume is in general (format 25 + completeness
15). The second half is a constant per profile, which is why the live DB sat at
mean 35.2 / max 75 and why "ATS >= 50" hid almost every job: the job-dependent half
was usually 0. The filter needs the job-dependent half, so it gets its own column.

Existing rows are zeroed rather than guessed: the cache key carries a scoring
version, so the first /api/jobs after this migration rebuilds the table with real
fit values.
"""

import logging

log = logging.getLogger(__name__)


def step(conn):
    """v9: ats_scores gains the job-dependent `fit` half of the score."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ats_scores)")}
    if "fit" not in cols:
        conn.execute("ALTER TABLE ats_scores ADD COLUMN fit REAL NOT NULL DEFAULT 0")
        log.info("v9: ats_scores.fit added — the cache rebuilds with real values "
                 "on the next /api/jobs (key carries the scoring version)")
    conn.commit()