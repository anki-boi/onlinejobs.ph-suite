"""v13: `keyword_hit` — why a keyword rule hid a job, or rescued it.

The rule engine already recorded THAT a job was filter-hidden; it never recorded
WHY. "hidden by keyword" with no keyword named is unfixable from the UI: you cannot
see that 'data entry' hid a ₱60,000/mo job because it also matched 'crypto'.

'' = no rule touched this job. Otherwise the matched word and the verdict:
  negative:crypto             — hidden because the listing mentions crypto
  no positive keyword         — hidden because it mentions none of the Keep words
  rescued:crypto ₱60,000/mo   — matched a Remove word but clears the pay goal, so it stays
"""

import logging

log = logging.getLogger("db.migrate.v13")


def step(conn) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "keyword_hit" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN keyword_hit TEXT DEFAULT ''")
    conn.commit()
    log.info("v13: jobs.keyword_hit added (the rule engine now says which word hit)")
