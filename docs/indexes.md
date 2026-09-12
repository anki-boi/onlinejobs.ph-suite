# Index rationale (W4.7)

`EXPLAIN QUERY PLAN` snapshots live in `tests/test_w47.py`; the indexes live in
`db/connection.py::_INDEX_DEFS` (fresh DBs) and `db/migrations/v7_perf_indexes.py`
(existing DBs). All are created only when their columns exist (legacy-DB safe).

| Index | Query it serves | Before → After |
|---|---|---|
| `idx_jobs_date_found (date_found, id)` | default list `ORDER BY date_found DESC, id DESC` | `SCAN jobs + TEMP B-TREE` → `SCAN jobs USING INDEX` (index-ordered walk) |
| `idx_jobs_status_datefound (status, date_found, id)` | status tab + default order | `SEARCH … (status=?) + TEMP B-TREE` → one index pass for filter **and** order |
| `idx_jobs_scrape_status_datefound (scrape_status, date_found, id)` | scrape-status filter + default order | same as above |
| `idx_jobs_salary_monthly_max (salary_monthly_max)` | "Min ₱/mo" filter (`salary_monthly_max >= ?`) | `SCAN jobs` → `SEARCH … (salary_monthly_max>?)` |

Deliberately **not** indexed:
- `search` / LIKE `%…%` — leading wildcard; FTS5 (W4.4) replaces this path.
- the `min_ats` `EXISTS` outer scan — inherent to the shape; the subquery side
  is index-backed via the `ats_scores(job_id, …)` primary key.
- `posted_date` — sorting uses `COALESCE(NULLIF(posted_date,''), date_found)`,
  which no index can serve; the date_found index covers the default ordering
  instead.

At the current scale (~5k rows) every indexed path is < 1 ms; these indexes
keep the main list O(log n + page) rather than O(n log n) at 50k rows.
