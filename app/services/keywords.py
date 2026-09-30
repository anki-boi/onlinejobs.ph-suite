"""
app/services/keywords.py — keyword auto-hide rules (W5.1: moved out of
server.py so the auto-run scheduler and the API share one implementation).

Jobs are matched on whatever text is available — title, company, skills, and
the description once enriched. Fresh (unenriched) stubs are included, so a new
scrape is filtered immediately, not only after enrichment. Keywords match
whole words (case-insensitive, simple plurals allowed): 'AI' matches 'AI',
'AI-powered' but never 'email'/'chain'. The positive rule only reaches jobs
still in 'New': a job the user has moved along is never auto-hidden.

Reversible: jobs hidden by this action are tagged (filter_hidden=1) with
their previous status. Re-applying with changed/removed keywords restores any
filter-hidden job the rules no longer hide. Jobs the user manually hid (or
manually re-statused) are never auto-restored.
"""

import re


def _get_haystack(row) -> str:
    """Build searchable text from all job fields."""
    return " ".join([
        row["title"] or "",
        row["description"] or "",
        row["company"] or "",
        row["skills"] or "",
    ]).lower()


def _keyword_regexes(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    """Compile whole-word, case-insensitive matchers, keeping the word each came from
    so a hide can be explained (X-C: `keyword_hit`).

    'ai' matches 'AI', 'Ai.', 'AI-powered' (and the simple plural 'ais') but
    not 'email', 'chain', 'maintenance'. Multi-word keywords match as phrases.
    The optional trailing 's' is only added when the keyword ends in a letter
    or digit, so 'call' matches 'calls' but not 'calling'.
    """
    out: list[tuple[str, re.Pattern]] = []
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        suffix = r"s?" if k[-1].isalnum() else ""
        out.append((k, re.compile(rf"(?<!\w){re.escape(k)}{suffix}(?!\w)", re.IGNORECASE)))
    return out


def stored_pay_goal(conn) -> float:
    """The stored pay goal in PHP/month (0 = off). A job that clears it is rescued
    from a Remove-keyword hit instead of vanishing (X-C / F2)."""
    from db.repos import settings as settings_repo
    try:
        return max(0.0, float(settings_repo.get(conn, "pay_goal_monthly", "") or 0))
    except ValueError:
        return 0.0


def _clears_goal(row, goal: float) -> bool:
    """Does the listing's own monthly figure reach the goal?

    Only a monthly figure the listing actually supports can rescue a job (see
    scraper/salary.py): an invented 160-hour month must not win an argument.
    """
    if not goal:
        return False
    monthly = row["salary_monthly_min"]
    return monthly is not None and monthly >= goal


def apply_keyword_filters(conn, positive: list[str], negative: list[str],
                          restore_all: bool = False,
                          pay_goal_monthly: float | None = None) -> tuple[int, int, int, int]:
    """Single pass over all jobs; batched writes; one commit.

    Rules per job:
      hide   — matches any negative keyword; or is 'New' (or was 'New' when
               filter-hidden) and positive keywords are set but none match.
      rescue — matched a negative keyword but its stated monthly pay clears the pay
               goal, so it stays visible and says why (F2: a ₱60,000/mo job that also
               mentions 'crypto' is still a job worth looking at).
      keep   — already Hidden: left as-is (user-hidden and filter-hidden both stay).
      restore — Hidden with filter_hidden=1 that the rules no longer hide goes
                back to its previous status.
    A job whose status the user manually changed (not Hidden) has its
    filter flag cleared and is treated as user-managed.
    Every job gets `keyword_hit`: '' when no rule touched it, otherwise the matched
    word and the verdict, so a hide is explainable from the table.
    Returns (hidden_by_negative, hidden_by_positive, restored, rescued).
    """
    neg_res = _keyword_regexes(negative)
    pos_res = _keyword_regexes(positive)
    goal = stored_pay_goal(conn) if pay_goal_monthly is None else max(0.0, pay_goal_monthly)

    rows = conn.execute(
        "SELECT id, status, filter_hidden, pre_filter_status, title, description, company, "
        "skills, salary_monthly_min, salary_assumed_currency, keyword_hit FROM jobs"
    ).fetchall()

    to_hide: list[tuple[int, str]] = []    # (id, previous status)
    to_restore: list[tuple[int, str]] = [] # (id, status to restore)
    flag_clears: list[int] = []
    hits: list[tuple[str, int]] = []
    neg_hidden = pos_hidden = restored = rescued = 0

    for row in rows:
        haystack = _get_haystack(row)
        status = row["status"]
        is_fh = bool(row["filter_hidden"])

        if restore_all:
            if status == "Hidden" and is_fh:
                to_restore.append((row["id"], row["pre_filter_status"] or "New"))
                restored += 1
            if row["keyword_hit"]:
                hits.append(("", row["id"]))
            continue

        neg_word = next((w for w, p in neg_res if p.search(haystack)), None)
        pos_applies = (
            status == "New"
            or (status == "Hidden" and is_fh and row["pre_filter_status"] == "New")
        )
        pos_match = any(p.search(haystack) for _, p in pos_res)

        if neg_word and _clears_goal(row, goal):
            # ≈ when the currency was guessed from magnitude (X-A): the rescue is
            # still "look at this", but the number is not the listing's own claim.
            approx = "≈" if row["salary_assumed_currency"] else ""
            hit = f"rescued:{neg_word} {approx}₱{row['salary_monthly_min']:,.0f}/mo"
            rescued += 1
        elif neg_word:
            hit = f"negative:{neg_word}"
        elif pos_applies and pos_res and not pos_match:
            hit = "no positive keyword"
        else:
            hit = ""
        should_hide = hit.startswith("negative:") or hit == "no positive keyword"

        if should_hide:
            if status == "Hidden":
                continue  # already hidden — user's or filter's, leave it
            to_hide.append((row["id"], status))
            if hit.startswith("negative:"):
                neg_hidden += 1
            else:
                pos_hidden += 1
        else:
            if status == "Hidden" and is_fh:
                to_restore.append((row["id"], row["pre_filter_status"] or "New"))
                restored += 1
            elif is_fh and status != "Hidden":
                # User manually re-statused a filter-hidden job — stop managing it
                flag_clears.append(row["id"])

        if row["keyword_hit"] != hit:
            hits.append((hit, row["id"]))

    if to_hide:
        conn.executemany(
            "UPDATE jobs SET status = 'Hidden', filter_hidden = 1, pre_filter_status = ? WHERE id = ?",
            [(pre, id) for id, pre in to_hide],
        )
        conn.executemany(
            "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, ?, 'Hidden')",
            [(id, pre) for id, pre in to_hide],
        )
    if to_restore:
        conn.executemany(
            "UPDATE jobs SET status = ?, filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
            [(new_status, id) for id, new_status in to_restore],
        )
        conn.executemany(
            "INSERT INTO job_history (job_id, old_status, new_status) VALUES (?, 'Hidden', ?)",
            [(id, new_status) for id, new_status in to_restore],
        )
    if flag_clears:
        conn.executemany(
            "UPDATE jobs SET filter_hidden = 0, pre_filter_status = '' WHERE id = ?",
            [(id,) for id in flag_clears],
        )
    if hits:
        conn.executemany("UPDATE jobs SET keyword_hit = ? WHERE id = ?",
                         [(hit, id) for hit, id in hits])
    if to_hide or to_restore or flag_clears or hits:
        conn.commit()
    return neg_hidden, pos_hidden, restored, rescued


def filter_hidden_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE filter_hidden = 1").fetchone()[0]
