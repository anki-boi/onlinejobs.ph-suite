"""
app/events.py — in-process pub/sub for server-generated events.

The pipeline endpoints stream to their own HTTP request, but the auto-run
scheduler has no request to stream into. It publishes here; the
/api/events SSE endpoint fans it out to any number of browser tabs, which
raise desktop notifications.
"""

import queue
import threading

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()


def publish(event: str, data) -> None:
    from app.sse import sse
    msg = sse(event, data)
    with _lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                _subscribers.remove(q)  # slow tab — drop it rather than buffer forever


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=200)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)
