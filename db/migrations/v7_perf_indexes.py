"""
V7: composite performance indexes (W4.7).

Rationale (EXPLAIN QUERY PLAN, 3000-row scratch DB):
- default list (ORDER BY date_found DESC) was a full SCAN + temp b-tree →
  (date_found, id) lets SQLite walk the index in order instead
- status / scrape_status filters + the same order were SEARCH + temp b-tree
  → (col, date_found, id) makes the filter and the order one index pass
- the salary floor filter (salary_monthly_max >= ?) was a full SCAN →
  (salary_monthly_max)
Every statement is IF NOT EXISTS, so re-running is a no-op.
"""


def step(conn):
    """v7: composite indexes for the main list queries (W4.7)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    indexes = [
        ("idx_jobs_date_found", "date_found, id", {"date_found"}),
        ("idx_jobs_status_datefound", "status, date_found, id", {"status", "date_found"}),
        ("idx_jobs_scrape_status_datefound", "scrape_status, date_found, id", {"scrape_status", "date_found"}),
        ("idx_jobs_salary_monthly_max", "salary_monthly_max", {"salary_monthly_max"}),
    ]
    for name, colspec, required in indexes:
        if required <= cols:  # every column present → create; else skip (no-op)
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON jobs({colspec})")
    conn.commit()
