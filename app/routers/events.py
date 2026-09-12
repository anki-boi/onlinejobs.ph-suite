"""app/routers/events.py — server-pushed SSE events. (W5.1 split; no
behaviour change.)"""

import queue as _queue

from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from app.sse import sse
from app import events as events_hub

router = APIRouter()


@router.get("/api/events")
def events_stream():
    """Server-pushed events (auto-run alerts, new jobs). One open stream per tab;
    browsers auto-reconnect, the hub drops dead subscribers."""
    q = events_hub.subscribe()

    def generate():
        try:
            yield sse("connected", "")
            while True:
                try:
                    yield q.get(timeout=15)
                except _queue.Empty:
                    yield sse("ping", "")  # keep proxies/tabs from dropping us
        finally:
            events_hub.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")
