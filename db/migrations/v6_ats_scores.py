"""
V6: materialized best-profile ATS scores (W4.3).

One row per job: the best-scoring resume profile and its total, rebuilt
whenever the jobs version counter or masters.json changes (same invalidation
key as the W2.3 per-process memo). `min_ats` becomes an EXISTS clause so
`total` is a truthful global count instead of a per-page Python filter.
"""


def step(conn):
    """v6: materialized best-profile ATS scores (ats_scores table)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ats_scores (
            job_id    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            profile   TEXT    NOT NULL,
            total     REAL    NOT NULL,
            updated_at TEXT   NOT NULL,
            PRIMARY KEY (job_id, profile)
        );
        """
    )
    conn.commit()
