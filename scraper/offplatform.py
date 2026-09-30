"""
scraper/offplatform.py — does this listing want you to leave the site?

Ported from ojph-cleaner/detail-text.js, which measured this on live listings.
Two findings from that measurement are the whole reason this file exists:

1. An ask is not a tool. 3 of 7 phrase hits in a live 18-listing sample were
   Telegram mentioned as *job content* ("Help monitor Telegram accounts"), not as
   an application route. A messaging tool therefore only counts inside a sentence
   that also asks you to apply.
2. OJ.ph strips application links but leaves the shape behind ("the form: ----------"),
   and people write addresses as "bob (at) example (dot) com" to get past scrapers.

Why the dashboard cares: a job that asks for WhatsApp/Google Forms/email pulls the
application off the platform you are tracking — the follow-up, the history and the
"did they reply" record all live somewhere you are not looking at. It is not a reason
to skip the job; it is a reason to know before you open it.
"""

from __future__ import annotations

import re

# Application asks: a request to submit or contact, never a mention of software the job uses.
ASK_PATTERNS = [
    r"(?:to|how to)\s+apply\b",
    r"apply\s+(?:here|via|through|at|using|by|thru|below|now|today)\b",
    r"fill\s+(?:out|up)\b[^.!?\n]{0,40}?\bform\b",
    r"complete\s+(?:the|this|our|your|an?)\b[^.!?\n]{0,40}?\bform\b",
    r"(?:application|intake|google|job)\s+form\b",
    r"(?:send|email|submit)\s+(?:your|us|me|over|in|the|it|all)\b[^.!?\n]{0,40}?"
    r"\b(?:resume|cv|application|portfolio|demo|reel|video|links?|work|samples?|form|details)\b",
    r"(?:resume|cv|application)\s+to\b",
    r"link\s+(?:to|of)\s+(?:your|my|the|our)\b[^.!?\n]{0,40}?"
    r"\b(?:portfolio|demo|reel|work|samples?|website|site|profile|drive|folder)\b",
    r"(?:link|form)\s+(?:below|provided|here|above)\b",
    r"(?:dm|pm|message)\s+me\b",
    r"contact\s+(?:me|us)\s+(?:at|via|through|on)\b",
]
_ASK_RES = [re.compile(p, re.IGNORECASE) for p in ASK_PATTERNS]

# Messaging/booking tools — only counted inside a sentence that is also asking (see above).
TOOL_RE = re.compile(
    r"\b(?:whatsapp|telegram|viber|wechat|signal|skype|discord|calendly|"
    r"book a call|schedule a call|google meet)\b", re.IGNORECASE)

_TLDS = ("com|net|org|ph|io|co|gle|ly|me|app|dev|xyz|info|biz|us|uk|site|page|link|tv|ai|so")
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'()\[\]]+"
    r"|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:" + _TLDS + r")\b(?:/[^\s<>\"'()\[\]]*)?",
    re.IGNORECASE)
EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?![a-z0-9-])",
                      re.IGNORECASE)
EMAIL_OBFUSCATED_RE = re.compile(
    r"[a-z0-9._%+-]+\s*(?:\(at\)|\[at\])\s*[a-z0-9.-]+\s*(?:\(dot\)|\[dot\])\s*[a-z]{2,}",
    re.IGNORECASE)
REDACTED_RE = re.compile(r"-{6,}")
OURSITE_RE = re.compile(r"onlinejobs\.ph", re.IGNORECASE)

MAX_LABELS = 4


def _sentence_ids(text: str) -> list[int]:
    """Which sentence each character belongs to.

    A '.' only ends a sentence when it is not inside a word: "hiring@gmail.com via
    Telegram" is one sentence, and splitting at the dot in "gmail.com" would put the
    tool in a different sentence from the ask that names it.
    """
    ids, s = [], 0
    for i, ch in enumerate(text):
        ids.append(s)
        if ch in "!?" or (ch == "." and not (i and text[i - 1].isalnum()
                                             and i + 1 < len(text) and text[i + 1].isalnum())):
            s += 1
    return ids


def _label_url(u: str) -> str:
    u = re.sub(r"^[a-z]+://", "", u, flags=re.IGNORECASE)
    u = re.sub(r"[.,;:!?\])]+$", "", u)
    return (u[:34] + "…") if len(u) > 35 else u


def off_platform(text: str | None) -> str:
    """Short label for what the listing wants you to do off-site, '' when nothing does.

    'ask:apply here · tool:telegram · url:forms.gle/abc' — readable in a tooltip,
    and empty means the application stays on the platform you are tracking.
    """
    if not text:
        return ""
    labels: list[str] = []
    ask_spans: list[tuple[int, int]] = []
    asks: list[str] = []
    for re_ in _ASK_RES:
        for m in re_.finditer(text):
            ask_spans.append((m.start(), m.end()))
            asks.append(m.group(0).strip().lower())
    if asks:
        # One ask per listing is enough to act on; three overlapping matches for the
        # same sentence would crowd out the address or tool that actually matters.
        labels.append("ask:" + max(asks, key=len))
    if ask_spans:
        sids = _sentence_ids(text)
        asking = {sids[i] for s, e in ask_spans for i in range(s, e)}
        for m in TOOL_RE.finditer(text):
            if any(sids[i] in asking for i in range(m.start(), m.end())):
                labels.append(f"tool:{m.group(0).lower()}")
    # Emails before URLs: "hiring@gmail.com" is an address, and URL_RE would happily
    # report the domain alone as a link worth visiting.
    email_spans = []
    for regex in (EMAIL_RE, EMAIL_OBFUSCATED_RE):
        for m in regex.finditer(text):
            if OURSITE_RE.search(m.group(0)):
                continue
            email_spans.append((m.start(), m.end()))
            labels.append(f"email:{m.group(0)}")
    for m in URL_RE.finditer(text):
        if any(st <= m.start() < en for st, en in email_spans):
            continue
        u = _label_url(m.group(0))
        if not OURSITE_RE.search(u):
            labels.append(f"url:{u}")
    if REDACTED_RE.search(text):
        labels.append("redacted link")

    seen, out = set(), []
    for label in labels:
        if label not in seen:
            seen.add(label)
            out.append(label)
    return " · ".join(out[:MAX_LABELS])


def over_40_hours(hours_text: str | None) -> bool:
    """Does the listing ask for more than a 40-hour week?

    Numbers above 40, '40+', or 'over/more than 40'. A 40-hour week is not flagged:
    full-time is not the thing worth warning about, the 60-hour week is.
    """
    if not hours_text:
        return False
    nums = [int(n) for n in re.findall(r"\d{1,3}", hours_text)]
    if any(n > 40 for n in nums):
        return True
    return bool(re.search(r"\b40\s*\+", hours_text, re.IGNORECASE)
                or re.search(r"\b(?:over|more than)\s+40\b", hours_text, re.IGNORECASE))
