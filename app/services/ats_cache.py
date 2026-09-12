"""
app/services/ats_cache.py — materialized best-profile ATS scores (W4.3).

The invalidation key is (jobs_version, masters.json mtime) — the exact key
the W2.3 per-process memo used, now persisted so it survives restarts and
shared threads. Rebuild is a full DELETE+INSERT: with ~1e3 jobs and
pre-loaded masters it runs in milliseconds, and it only runs when the key
changes, never per request.
"""

KEY = "ats_scores_key"


def ensure_fresh(conn, key: str, masters: dict, job_rows, job_dict) -> None:
    """Rebuild ats_scores if the key changed. `job_dict` converts a jobs
    row to the dict best_profile_for_job expects (server supplies it to
    avoid a circular import)."""
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (KEY,)
    ).fetchone()
    if row is not None and row["value"] == key:
        return

    import time
    from resumes import schema as resume_schema

    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    out = []
    for r in job_rows:
        best = resume_schema.best_profile_for_job(masters, job_dict(r))
        if best is None:
            continue
        name, sc = best
        out.append((r["id"], name, sc["total"], now))

    conn.execute("DELETE FROM ats_scores")
    conn.executemany(
        "INSERT INTO ats_scores (job_id, profile, total, updated_at) "
        "VALUES (?, ?, ?, ?)",
        out,
    )
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (KEY, key),
    )
    conn.commit()
