# Job Hunter Dashboard

Local job tracking dashboard for OnlineJobs.ph.

## Setup

```bash
cd job_hunter
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000

## Workflow

1. **Enter Search Keyword** — search jobs by keyword (e.g., "medical", "bookkeeping", "designer")
2. **Set Posted Since** (optional) — only scrape jobs posted after a specific date
3. **Add Keywords** — positive (keep) and negative (hide) filters applied post-scrape
4. **Run Pipeline** — harvests links, then fetches full details for each job
5. **Apply Keyword Filters** — hide jobs that don't match your positive keywords or match negative keywords

## Notes

- **Tag searching is deprecated** — use keyword search instead
- Hidden jobs stay in the DB for deduplication — they won't resurface in future scrapes
- Re-check Open Jobs re-fetches details for all New/Open jobs (useful after a partial run)
- Click any row to open the detail modal — update status, add notes

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend, all API routes |
| `db.py` | All SQLite access |
| `scraper.py` | Scraping logic (tags, links, job details) |
| `static/app.js` | Frontend logic |
| `static/style.css` | Styling |
| `templates/index.html` | Dashboard HTML |
| `jobs.db` | SQLite database (auto-created) |
