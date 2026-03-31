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

1. **Refresh Tags** — sync the full skill-tag catalogue from OnlineJobs.ph
2. **Select tags** from the dropdown (filter the list with the search box)
3. **Set pages per tag** — how deep to scrape per tag (1 page = ~30 jobs)
4. **Add keywords** — positive (keep) and negative (hide) applied post-scrape
5. **Run Pipeline** — harvests links, then fetches full details for each
6. **Apply Keyword Filters** — hides non-matching jobs from the dashboard

## Notes

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
