# Scraping: politeness & terms of service

This is an **unofficial, personal-use** tool. OnlineJobs.ph does not
provide an official API for job search, so the scraper makes ordinary
HTTP requests to the public site. Be a good guest:

## What the scraper sends

- **One request per page or detail page.** Harvesting walks the search
  results list page by page; the enrich phase fetches detail pages
  (up to `enrich_workers` in parallel, default 3 — detail pages only;
  list pages are never fetched in parallel).
- **Global 1-second throttle** (`request_delay`) between *all* outbound
  requests. Don't lower it below 1 s.
- **A normal browser User-Agent** (`user_agent` in config.json) — the
  site is a public website, and we ask like a browser, not a spider.
- **Retries with exponential backoff on 429/5xx**, honoring the
  server's `Retry-After` header when present (`max_retries`, default 3).
  An exhausted retry sequence fails that one request, not the whole
  run: the run logs the error, keeps the partial results, and the next
  run (manual or the 4-hour auto-run) picks the rest up — dedup means
  nothing is re-inserted.

## What it fetches

| Phase | Pages |
|---|---|
| Harvest | Search results list (paginated), scoped to your keywords/categories/skills |
| Enrich | Detail pages for new jobs, plus jobs whose last check is older than `enrich_interval_days` |
| Check for updates | Detail pages only (same staleness rule) |
| First run | Skills taxonomy API (one bulk fetch) |

Typical full-board harvest: a few dozen list pages + one detail page
per new job. The auto-run (default every 4 h) keeps volume low because
most jobs are already known and deduped.

## Closed-listing detection

A detail page that returns 404/410, or whose content says the listing
is no longer available, marks the job **Closed** (site-side state,
shown by the dot in the table). Your status and notes are preserved.

## Structure-change watchdog

If the search page stops yielding job boxes while the site still
claims results, the run raises a **structure change** alert instead of
silently recording zero — the site's markup probably changed, and
`scraper/parsers.py` needs a look against a saved page.

## Terms of service — read before running

The site's terms govern what you may do with their data. In practice,
for this tool:

1. **Personal use only.** The tracker is for your own job search; the
   database is gitignored and never leaves your machine.
2. **No load.** Keep the throttle at ≥ 1 s, the enrich workers at
   ≤ 3, and the auto-run at ≥ 1 h. Don't run several instances against
   the site at once.
3. **No scraping of personal data.** The tool stores job postings
   (title, employer, salary, location, description, skills) — the
   public content of the listings. It does not log in or scrape
   profiles/messages. (If you add `oj_cookies`, keep them for your own
   session and treat them as sensitive: `config.local.json` is
   gitignored and redacted in `/api/config`.)
4. **If asked, stop.** The site is private property. If OnlineJobs.ph
   asks you to stop, or adds bot protection that makes the 1 s pace
   fail, back off (longer delay, longer interval) or discontinue.
5. **Attribution if you publish.** If you ever republish data from
   this tracker (blog, dataset), credit OnlineJobs.ph as the source.

## Reporting bugs

When the layout changes, save a copy of the affected page
(devtools → save as HTML) — that's the fastest way to fix
`scraper/parsers.py`.
