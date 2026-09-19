# Problems this solves

## The short version

**Applying to jobs is cheap. Knowing which jobs to apply to is the whole job.**

A job hunt has two costs: finding listings, and deciding what to do with them. The board
gives you plenty of listings. The deciding is where the hours go, and it's the part nobody
has tooling for — so it happens in a browser, in your head, and in a spreadsheet that goes
stale.

This dashboard exists because I was doing that hunt and the process was burning the exact
resource I needed for it.

---

## The problems, in the order they cost time

### 1. No fit signal — so you apply blind or don't apply

A listing tells you the title and a company. It doesn't tell you whether *your* résumé
matches it. So the decision becomes a feeling, and feelings degrade over a list of hundreds.

→ **A deterministic ATS score per listing** — `skills 40 + keywords 20 + format 25 +
completeness 15`, returned with the matched *and missing* skills and rule-based
suggestions. Shown in the job drawer, filterable with **ATS ≥ 50** to collapse the list to
the jobs worth reading.

→ **Deliberately not an LLM scorer.** Recruiter-side first passes are keyword and structure
based, so a rule scorer models the actual thing. More importantly, **a rule scorer cannot
hallucinate a match**, and a fit score you can't trust is worse than no score — you'd
either ignore it or believe it wrongly.

→ **Multiple track profiles.** One résumé can't be best-fit for clinical data automation,
healthcare, and tech data roles at once. Named profiles live in one file, and `auto=1`
scores every profile and returns the best fit, so the right track is always the one being
judged.

### 2. Tailoring per application is manual, so nobody does it

Tailoring a résumé per listing is 20–40 minutes of rewriting. Multiply by the number of
applications worth making, and the rational move is to send one generic résumé everywhere —
which is exactly what everyone does, and why most applications are mediocre.

→ **One-click tailoring** that rewrites and reorders the best-fitting profile for that job.

→ **Rewrite and reorder only — never invent facts**, written in the owner's tone (short, no
buzzwords). This is a trust boundary: a résumé generator that embellishes is a liability,
not a feature. Invalid or failed output **falls back to the profile** rather than shipping
something wrong.

→ **Export as docx/txt** — one column, standard headings, real bullets, because that's the
layout ATS parsers actually chew. PDF deliberately skipped (docx is what ATS want).

### 3. A two-page CV fails a one-page rule, and you find out by never hearing back

Uploading a two-page CV is the single most common silent rejection. Word processors make it
easy to accidentally be 1.2 pages, and nothing tells you.

→ **A 1-page CV builder (Harvard template)** that digests every résumé file in a configured
folder (PDF/DOCX/TXT/MD/JSON), drafts RenderCV YAML from those facts only, renders it, and
**iterates until it is exactly one page** (max 4 rounds) — **it never ships a two-pager.**

### 4. Salaries quoted in currencies you convert in your head

`US$800/mo`, `₱30,000`, `$6/hr` in the same list. Comparison requires mental arithmetic at
the exact moment you're trying to decide.

→ **Parsed into monthly min/max and shown as a `₱…/mo` / `US$…/mo` chip**
(hourly×160, weekly×4.33, daily×30, annual÷12). The conversion rate used is **recorded in
the database with a timestamp and displayed**, so a number you acted on six weeks ago can be
explained rather than guessed at.

### 5. You apply to jobs that already closed

Listings don't announce themselves as dead. You find out when the page 404s — after you've
spent the effort.

→ **Stale detail pages are re-checked** on schedule. Deleted listings (HTTP 404/410, or
"no longer available" wording) are marked **Closed**, and closures stream to you as an
alert.

### 6. The same job shows up repeatedly

Employers repost. The same role reappears as a new listing and you screen it again from
scratch, because nothing tells you it's the same thing.

→ **Repost detection**: same title, same employer → a `↻ repost` badge pointing at the
original, with a **Hide reposts** toggle.

### 7. The tracker goes stale, so you stop trusting it

A spreadsheet tracker is only as good as your discipline, and discipline fails exactly when
you're busiest. Then the tracker is wrong, and a wrong tracker is worse than none.

→ **Auto-run on an interval (1–24 h)** harvests the board and re-checks new/stale jobs on
schedule, so the list is fresh without you remembering.

→ **Desktop alerts** for new-job batches, closures, salary changes and **due follow-ups** —
the tracker tells you when to act instead of waiting to be opened.

→ **Saved keyword rules auto-apply to every fresh run**, with an alert when anything is
hidden. The filtering you set up once keeps working.

### 8. The scraper breaks silently and records zero jobs as truth

This is the failure that matters most, and it's the same lesson the userscript suite learned
the hard way: **when a site changes its markup, a scraper doesn't error — it returns
nothing, and nothing looks like "no new jobs today".**

→ **A parse watchdog**: if the search page stops yielding job boxes while the site claims
results, the run logs a **`structure change` alert** instead of silently recording zero.
A silent zero is the failure mode this exists to kill.

### 9. Hammering a site that isn't yours

Automated scraping that ignores the site's tolerance gets accounts banned and is simply
rude.

→ **Throttled to 1 request/second**, exponential backoff on 429/5xx, and server
`Retry-After` headers respected. Politeness budget documented in `docs/scraping.md`.

### 10. Six months of tracking lost to one bad write

→ **`VACUUM INTO` snapshots with retention pruning** (default 7), restorable by copying a
snapshot back over the database.

---

## What it replaced

| Before | After |
|---|---|
| Scrolling and opening a hundred tabs to reject ninety | Keyword/category/skill scoping, column funnels, date-range and salary filters |
| "Does my résumé fit this?" — a feeling | Deterministic ATS score with matched/missing skills, filterable at ≥ 50 |
| One generic résumé sent to everything | One-click tailoring per listing, facts-only, exporting ATS-friendly docx |
| A two-page CV uploaded into a one-page ATS rule | A builder that iterates until it is exactly one page |
| Mixed currencies compared mentally | Normalized monthly chips with the rate and its timestamp recorded |
| Finding out a job closed after applying | Scheduled re-checks; closures marked and alerted |
| Re-screening reposted jobs | `↻ repost` detection with a hide toggle |
| A spreadsheet tracker that goes stale | Auto-run + desktop alerts on new jobs, closures, salary changes, due follow-ups |
| A markup change silently recording zero jobs | A parse watchdog that raises a `structure change` alert |
| Manually re-applying your filters after every refresh | Saved server-side and auto-applied to every run |
| No idea whether the tool was even working | `/health` probing DB and site with degraded states |

---

## Engineering decisions worth pointing at

- **359 tests, no network.** The suite covers DB, parsers, pipeline, API, salary maths,
  résumé rendering and YAML CV. `tools/gate.sh` runs ruff + the full suite + a
  personal-path scan + **a README-truthfulness check** (`tools/check_readme.py` verifies
  every config key is documented and no counts are stale).
- **Migrations are versioned and dry-runnable** — `python -m db.migrate --dry-run` shows
  what a database would do before it does it.
- **A real error contract**: every non-2xx response is
  `{"error": {"code", "message", "detail"}}`, and a user-initiated stop is recorded as
  `stopped` with partial results kept — **never `failed`**. Conflating "you stopped it" with
  "it broke" is how you stop trusting your own status fields.
- **Graceful shutdown**: first `Ctrl-C` stops the in-flight run and waits up to 10 seconds;
  a second exits immediately.
- **Live config reload** without a restart, with secrets redacted in `GET /api/config`.
- **Pagination is server-side** — the table never loads thousands of rows into the browser.
- **Runs against a copy of the real DB** for testing (`JOBS_DB_PATH`), because parser tests
  against invented fixtures don't catch real-world format drift.

## Scope and limits

- **Local by design.** FastAPI + SQLite on your machine, `127.0.0.1:8371`. Your résumé and
  your data stay local; only code is in the repo.
- **Single-user.** No accounts, no multi-tenancy, no hosting story — it's a personal tool.
- **Not open source.** Public to read; no license is granted.
- Scraping a site you don't own is subject to its terms — see `docs/scraping.md`.
