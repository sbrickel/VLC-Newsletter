"""Optional LLM-assisted event extraction for prose-heavy sources.

Some sites (Visit Valencia, Valenciabonita, ...) publish SEO guide articles
*about* events using generic Article schema, with the real event date only
mentioned in body text ("programado hasta el 26 de julio") rather than any
machine-readable field. A regex/heuristic scraper can't reliably read that;
an LLM can. Uses Groq's free-tier API (https://console.groq.com) — no-op
if GROQ_API_KEY isn't set, so the pipeline runs identically without it.
"""
import json
import logging
import os
import threading
import time

import requests

log = logging.getLogger("llm_extractor")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.1-8b-instant"
TIMEOUT = (6, 20)

# The free tier's real constraint is tokens/minute (6000 TPM for this
# model), not a fixed requests/minute count — each call's prompt (article
# text + template) runs several hundred tokens, so a naive fixed interval
# based on request count alone still hits 429s. Pace adaptively instead,
# reading Groq's live quota headers after every response.
MIN_INTERVAL = 1.0
ESTIMATED_TOKENS_PER_CALL = 900
MAX_RETRIES = 3

_next_allowed = 0.0
_lock = threading.Lock()


def _parse_duration(value):
    """Parse Groq's rate-limit reset durations, e.g. "6s" or "410ms"."""
    try:
        value = value.strip()
        if value.endswith("ms"):
            return float(value[:-2]) / 1000
        if value.endswith("s"):
            return float(value[:-1])
    except (ValueError, AttributeError):
        pass
    return 2.0


def _update_pacing(headers, min_wait=0.0):
    global _next_allowed

    wait = MIN_INTERVAL
    try:
        remaining_tokens = int(headers.get("x-ratelimit-remaining-tokens", "999999"))
        if remaining_tokens < ESTIMATED_TOKENS_PER_CALL:
            # Not enough budget left for another call — wait out the reset.
            wait = _parse_duration(headers.get("x-ratelimit-reset-tokens", "0s"))
    except ValueError:
        pass

    wait = max(wait, min_wait)

    with _lock:
        _next_allowed = max(_next_allowed, time.monotonic() + wait)


PROMPT_TEMPLATE = """Today's date is {today}. Below is the title and body text of an article from a Valencia (Spain) events/tourism website.

Determine whether it describes ONE specific upcoming event with a real, EXPLICITLY STATED date — not a vague seasonal roundup ("this summer"), a past/lapsed event, or a generic guide with no fixed date.

Do NOT guess or default to today's date. If the text does not explicitly state a specific day (or date range), set is_event to false and start_date to null — even if the topic sounds timely.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"is_event": true or false, "title": "short event name", "start_date": "YYYY-MM-DD" or null}}

If the article covers several different events, pick the single most prominent one that has an explicit date. If unsure, set is_event to false.

Title: {title}
Text: {text}
"""

LISTING_PROMPT_TEMPLATE = """Today's date is {today}. Below is text scraped from a Valencia (Spain) institution's events/agenda page, listing several upcoming activities as plain text (no HTML structure to lean on).

Extract EVERY distinct event that has an explicit date. Ignore navigation, category/date filter widgets, and anything without a specific date attached.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"events": [{{"title": "short event name", "start_date": "YYYY-MM-DD"}}, ...]}}

If an event gives a date range ("del 15 de junio al 30 de septiembre"), use the start of the range. If there are no real dated events, return {{"events": []}}.

Text: {text}
"""

PUBLIC_CHECK_PROMPT = """Below is the description of an event or activity, titled "{title}".

Determine whether it is OPEN TO THE GENERAL PUBLIC — anyone can attend or take part — as opposed to being RESTRICTED to a closed group (e.g. only enrolled students, only staff/employees, invitation-only, internal to one organization).

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"open_to_public": true or false}}

If the text doesn't clearly state a restriction, default to true (assume open).

Text: {text}
"""


def available():
    return bool(GROQ_API_KEY)


def _pace():
    with _lock:
        wait = _next_allowed - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _call_groq(prompt, label=""):
    """POST a prompt to Groq with adaptive pacing/retry. Returns the parsed
    JSON response body, or None on failure."""

    if not GROQ_API_KEY:
        return None

    for attempt in range(MAX_RETRIES):
        _pace()
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
                timeout=TIMEOUT,
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 5))
                _update_pacing(resp.headers, min_wait=retry_after)
                log.info("rate limited, waiting %.1fs (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            _update_pacing(resp.headers)
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:
            log.warning("LLM call failed (%s)", label, exc_info=True)
            return None

    return None


def extract_event(title, text, today):
    """Returns {"title": str, "start_date": "YYYY-MM-DD"} or None."""

    prompt = PROMPT_TEMPLATE.format(today=today.isoformat(), title=title, text=text[:2000])
    data = _call_groq(prompt, label=title)

    if not data or not data.get("is_event") or not data.get("start_date"):
        return None

    return {
        "title": (data.get("title") or title).strip(),
        "start_date": data["start_date"],
    }


def extract_events_from_listing(text, today):
    """For pages with no per-card markup at all (plain-text agenda listings):
    ask the LLM to pull out every distinct dated event in one pass. Returns
    a list of {"title": str, "start_date": "YYYY-MM-DD"} dicts."""

    prompt = LISTING_PROMPT_TEMPLATE.format(today=today.isoformat(), text=text[:4000])
    data = _call_groq(prompt, label="listing")

    if not data or not isinstance(data.get("events"), list):
        return []

    results = []
    for item in data["events"]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        start_date = item.get("start_date")
        if title and start_date:
            results.append({"title": title, "start_date": start_date})

    return results


def check_open_to_public(title, text):
    """Whether an event's own description indicates it's open to the
    general public rather than restricted to a closed group. Defaults to
    True (keep it) if the check is unavailable or inconclusive — this
    should only filter out events that clearly state a restriction, not
    silently drop anything we're merely unsure about."""

    prompt = PUBLIC_CHECK_PROMPT.format(title=title, text=text[:1500])
    data = _call_groq(prompt, label=f"public-check: {title}")

    if not data:
        return True

    return bool(data.get("open_to_public", True))
