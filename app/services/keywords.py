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


def _keyword_regexes(keywords: list[str]) -> list[re.Pattern]:
    """Compile whole-word, case-insensitive matchers for keyword filtering.

    'ai' matches 'AI', 'Ai.', 'AI-powered' (and the simple plural 'ais') but
    not 'email', 'chain', 'maintenance'. Multi-word keywords match as phrases.
    The optional trailing 's' is only added when the keyword ends in a letter
    or digit, so 'call' matches 'calls' but not 'calling'.
    """
    out: list[re.Pattern] = []
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        suffix = r"s?" if k[-1].isalnum() else ""
        out.append(re.compile(rf"(?<!\w){re.escape(k)}{suffix}(?!\w)", re.IGNORECASE))
    return out


def apply_keyword_filters(conn, positive: list[str], negative: list[str],
                          restore_all: bool = False) -> tuple[int, int, int]:
    """Single pass over all jobs; batched writes; one commit.

    Rules per job:
      hide  — matches any negative keyword; or is 'New' (or was 'New' when
              filter-hidden) and positive keywords are set but none match.
      keep  — already Hidden: left as-is (user-hidden and filter-hidden both stay).
      restore — Hidden with filter_hidden=1 that the rules no longer hide goes
                back to its previous status.
    A job whose status the user manually changed (not Hidden) has its
    filter flag cleared and is treated as user-managed.
    Returns (hidden_by_negative, hidden_by_positive, restored).
    """
    neg_res = _keyword_regexes(negative)
    pos_res = _keyword_regexes(positive)

    rows = conn.execute(
        "SELECT id, status, filter_hidden, pre_filter_status, title, description, company, skills "
        "FROM jobs"
    ).fetchall()

    to_hide: list[tuple[int, str]] = []    # (id, previous status)
    to_restore: list[tuple[int, str]] = [] # (id, status to restore)
    flag_clears: list[int] = []
    neg_hidden = pos_hidden = restored = 0

    for row in rows:
        haystack = _get_haystack(row)
        status = row["status"]
        is_fh = bool(row["filter_hidden"])

        if restore_all:
            if status == "Hidden" and is_fh:
                to_restore.append((row["id"], row["pre_filter_status"] or "New"))
                restored += 1
            continue

        neg_match = any(p.search(haystack) for p in neg_res)
        pos_applies = (
            status == "New"
            or (status == "Hidden" and is_fh and row["pre_filter_status"] == "New")
        )
        pos_match = any(p.search(haystack) for p in pos_res)
        should_hide = neg_match or (pos_applies and bool(pos_res) and not pos_match)

        if should_hide:
            if status == "Hidden":
                continue  # already hidden — user's or filter's, leave it
            to_hide.append((row["id"], status))
            if neg_match:
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
    if to_hide or to_restore or flag_clears:
        conn.commit()
    return neg_hidden, pos_hidden, restored


def filter_hidden_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE filter_hidden = 1").fetchone()[0]
