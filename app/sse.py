"""
app/sse.py — SSE (Server-Sent Events) helper for streaming pipeline output.

W2.2: every stream is a "run" — hardened() announces a run id up front,
tags every event with it (SSE `id:` line, so reconnects can resume via
Last-Event-ID and the UI can send it to /api/pipeline/stop), and guarantees
the stream ends with `error` + `done` even when the work raises mid-stream.
"""

import json
import logging
import uuid

log = logging.getLogger(__name__)


def sse(event: str, data, run_id: str | None = None) -> str:
    """Format a single SSE message.

    `run_id`, when present, is emitted as the SSE `id:` line (W2.2) and
    injected into dict payloads so clients can correlate events even
    without Last-Event-ID support.
    """
    if isinstance(data, dict) and run_id is not None and "run_id" not in data:
        data = {**data, "run_id": run_id}
    if isinstance(data, (dict, list)):
        data = json.dumps(data, default=str)
    payload = f"event: {event}\ndata: {data}\n"
    if run_id:
        payload = f"id: {run_id}\n" + payload
    return payload + "\n"


def hardened(gen, name: str):
    """Wrap a streaming generator into a resilient "run" (W2.2).

    - announces `run_started {run, run_id}` as the first event
    - tags every inner event with the run id (events already carrying an
      `id:` line pass through unchanged)
    - if the inner generator raises, emits `error {detail}` then
      `done "failed"` — the stream always terminates in a known state
    """

    def wrapped():
        run_id = uuid.uuid4().hex
        try:
            yield sse("run_started", {"run": name, "run_id": run_id}, run_id)
            for chunk in gen:
                if "id: " not in chunk:
                    chunk = f"id: {run_id}\n{chunk}"
                yield chunk
        except Exception as e:
            log.exception("run '%s' (%s) failed: %s", name, run_id, e)
            yield sse("error", {"detail": str(e), "run": name}, run_id)
            yield sse("done", "failed", run_id)

    return wrapped()
