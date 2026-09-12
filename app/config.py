"""
app/config.py — single owner of the live config (W1.4).

config.json (checked in, defaults) is overlaid by config.local.json
(gitignored — secrets, personal paths). reload() re-reads both and
atomically replaces the live state; every read goes through get(), so
a changed file takes effect on reload with no restart — the LLM
tailoring config is the motivating case.
"""

import json
import threading

from db import connection as dbconn

_lock = threading.Lock()
_live: dict | None = None


def _read_disk() -> dict:
    cfg_path = dbconn.BASE_DIR / "config.json"
    cfg: dict = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    local = dbconn.BASE_DIR / "config.local.json"
    if local.exists():
        cfg = {**cfg, **json.loads(local.read_text())}
    return cfg


def get() -> dict:
    """The live merged config (loaded once, replaced only by reload())."""
    global _live
    with _lock:
        if _live is None:
            _live = _read_disk()
        return _live


def reload() -> dict:
    """Force a re-read from disk; returns the new live state."""
    global _live
    with _lock:
        _live = _read_disk()
        return _live


def redacted() -> dict:
    """Config for display/API — secret values masked."""
    out = dict(get())
    for key in ("llm_api_key", "oj_cookies"):
        if out.get(key):
            out[key] = "***"
    return out


def features_off() -> list[str]:
    """Optional features currently off, as user-actionable notes (UI banner)."""
    cfg = get()
    off = []
    if not cfg.get("llm_base_url"):
        off.append(
            "LLM tailoring is off — set llm_base_url (and llm_api_key if needed) "
            "in config.local.json, then press 'Reload config'."
        )
    return off
