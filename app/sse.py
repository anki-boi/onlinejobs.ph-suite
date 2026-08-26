"""
app/sse.py — Server-Sent Events helpers.
"""

import json


def sse(event: str, data: str | dict) -> str:
    """Format a single SSE message."""
    if isinstance(data, dict):
        data = json.dumps(data, default=str)
    return f"event: {event}\ndata: {data}\n\n"
