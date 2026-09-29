"""
V10: tailored resumes get stored (audit B9).

`GET /api/resume/export?tailored=1` used to call tailor() again — a second LLM
call at temperature 0.2 — so the .docx you downloaded was a different document
from the one whose ATS score you had just been shown. The tailored profile is now
written once, keyed by (job_id, profile), and export serves the stored document.
"""

import logging

log = logging.getLogger(__name__)


def step(conn):
    """v10: store the tailored resume so download == score you saw."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tailored_resumes (
            job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            profile      TEXT    NOT NULL,
            jobs_version INTEGER NOT NULL DEFAULT 0,
            doc          TEXT    NOT NULL,
            created_at   TEXT    NOT NULL,
            PRIMARY KEY (job_id, profile)
        )
        """
    )
    log.info("v10: tailored_resumes ready")
    conn.commit()