"""Generic event scraper: tries JSON-LD, then RSS, then ICS, then heuristic HTML."""
import json
import os
import re
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from icalendar import Calendar
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import llm_extractor
from classifier import is_recurring

log = logging.getLogger("scraper")


def _setup_local_playwright_libs():
    """`playwright install --with-deps chromium` (used in CI) needs sudo to
    apt-install Chromium's system libraries. In a dev environment without
    root, `scripts/setup_playwright_libs.sh` extracts the same packages into
    .playwright-libs/ instead — point LD_LIBRARY_PATH at it if present."""

    libdir = os.path.join(
        os.path.dirname(__file__), "..", ".playwright-libs",
        "extracted", "usr", "lib", "x86_64-linux-gnu",
    )
    libdir = os.path.abspath(libdir)
    if not os.path.isdir(libdir):
        return

    current = os.environ.get("LD_LIBRARY_PATH", "")
    if libdir not in current.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{libdir}:{current}" if current else libdir


_setup_local_playwright_libs()
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
TIMEOUT = (6, 15)  # (connect_timeout, read_timeout) — fail fast on dead domains

_session = requests.Session()
_session.headers.update(HEADERS)
_retry = Retry(total=1, connect=1, read=1, backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504])
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))


from dataclasses import dataclass
import time

@dataclass
class ScrapeResult:
    events: list
    parser: str = ""
    confidence: float = 0.0
    success: bool = False
    error: str | None = None


class ScrapeStats:
    def __init__(self):
        self.parser = None
        self.events_found = 0
        self.fetch_ms = 0
        self.failed_reason = None


def fetch(url):
    start = time.perf_counter()

    try:
        response = _session.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        elapsed = int((time.perf_counter() - start) * 1000)

        log.info(
            "FETCH %-35s %4d ms",
            url[:35],
            elapsed
        )

        return response

    except requests.exceptions.HTTPError as e:

        log.info(
            "HTTP %-35s %s",
            url[:35],
            e.response.status_code
        )

    except requests.exceptions.Timeout:

        log.info(
            "TIMEOUT %-35s",
            url[:35]
        )

    except requests.exceptions.ConnectionError:

        log.info(
            "CONNECT %-35s",
            url[:35]
        )

    except requests.RequestException as e:

        log.info(
            "REQUEST %-35s %s",
            url[:35],
            str(e)
        )

    return None

ISO_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")

# dateutil's parser only recognizes English month names/abbreviations —
# Spanish abbreviations happen to coincide for a few (jun, jul, ago-ish)
# but FULL Spanish month words ("julio", "septiembre", ...) are silently
# unrecognized. When that happens dateutil doesn't error; it just treats
# the day-number as if it were the month and defaults the day to *today's*
# day, producing a wrong-but-plausible-looking date with no warning. Fix
# by translating Spanish month words to English before parsing.
SPANISH_TO_ENGLISH_MONTH = {
    "enero": "January", "ene": "January",
    "febrero": "February", "feb": "February",
    "marzo": "March", "mar": "March",
    "abril": "April", "abr": "April",
    "mayo": "May",
    "junio": "June", "jun": "June",
    "julio": "July", "jul": "July",
    "agosto": "August", "ago": "August",
    "septiembre": "September", "setiembre": "September", "sep": "September", "sept": "September",
    "octubre": "October", "oct": "October",
    "noviembre": "November", "nov": "November",
    "diciembre": "December", "dic": "December",
}
SPANISH_MONTH_WORD_RE = re.compile(
    r"\b(" + "|".join(SPANISH_TO_ENGLISH_MONTH) + r")\.?\b",
    re.IGNORECASE,
)


def _translate_spanish_months(text):
    return SPANISH_MONTH_WORD_RE.sub(
        lambda m: SPANISH_TO_ENGLISH_MONTH[m.group(1).lower()], text
    )


def parse_date(value):
    text = _translate_spanish_months(str(value).strip())
    # ISO dates (YYYY-MM-DD, from JSON-LD/RSS/ICS) are unambiguous; dayfirst=True
    # would otherwise swap month/day whenever the day is <=12. Only ambiguous
    # formats like Spanish DD/MM/YYYY need dayfirst.
    dayfirst = not ISO_DATE_RE.match(text)
    try:
        dt = dateparser.parse(text, fuzzy=True, dayfirst=dayfirst)
    except Exception:
        return None
    if dt and dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)  # normalize to naive for consistent comparisons
    return dt


def from_jsonld(soup, base_url):
    events = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            graph = item.get("@graph") if isinstance(item, dict) else None
            candidates = graph if graph else [item]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                # schema.org defines many Event subtypes (BusinessEvent,
                # SocialEvent, MusicEvent, ScreeningEvent, ...) that all carry
                # the same startDate/name semantics — match any of them.
                type_val = c.get("@type")
                types = type_val if isinstance(type_val, list) else [type_val]
                if not any(isinstance(t, str) and t.endswith("Event") for t in types):
                    continue

                start = parse_date(c.get("startDate"))
                if not start:
                    continue
                events.append({
                    "title": c.get("name", "").strip(),
                    "date": start,
                    "url": c.get("url") or base_url,
                    "description": (c.get("description") or "")[:300],
                    })
    return events

def from_next_data(soup):

    script = soup.find("script", id="__NEXT_DATA__")

    if not script:
        return []

    try:
        data = json.loads(script.string)
    except Exception:
        return []

    events = []

    def walk(obj):

        if isinstance(obj, dict):

            if "startDate" in obj and "name" in obj:

                date = parse_date(obj["startDate"])

                if date:

                    events.append({
                        "title": obj["name"],
                        "date": date,
                        "url": obj.get("url", ""),
                        "description": obj.get("description", "")[:300],
                    })

            for v in obj.values():
                walk(v)

        elif isinstance(obj, list):

            for item in obj:
                walk(item)

    walk(data)

    return events

def from_nuxt_data(soup):

    scripts = soup.find_all("script")

    for script in scripts:

        text = script.string or ""

        if "__NUXT__" not in text:
            continue

        matches = re.findall(r"\{.*\}", text, re.S)

        for m in matches:

            try:
                data = json.loads(m)
            except Exception:
                continue

            events = []

            def walk(obj):

                if isinstance(obj, dict):

                    if "startDate" in obj and "name" in obj:

                        date = parse_date(obj["startDate"])

                        if date:

                            events.append({
                                "title": obj["name"],
                                "date": date,
                                "url": obj.get("url", ""),
                                "description": "",
                            })

                    for v in obj.values():
                        walk(v)

                elif isinstance(obj, list):

                    for item in obj:
                        walk(item)

            walk(data)

            if events:
                return events

    return []

def from_embedded_json(soup):

    events = []

    for script in soup.find_all("script"):

        text = script.string

        if not text:
            continue

        if "startDate" not in text:
            continue

        try:
            data = json.loads(text)
        except Exception:
            continue

        if isinstance(data, list):

            for obj in data:

                if not isinstance(obj, dict):
                    continue

                if "name" not in obj:
                    continue

                date = parse_date(obj.get("startDate"))

                if not date:
                    continue

                events.append({
                    "title": obj["name"],
                    "date": date,
                    "url": obj.get("url", ""),
                    "description": "",
                })

    return events


VALENCIA_ES_CONTENT_BASE = "https://www.valencia.es/cas/agenda-de-la-ciudad/-/content/"
VALENCIA_ES_EVENTS_RE = re.compile(r"var\s+eventosInicio\s*=\s*(\[.*?\]);", re.S)


def from_valencia_es_events(response_text):
    """valencia.es's agenda listing page needs no JS rendering at all — the
    full event list is already embedded as a JS array literal
    (`eventosInicio`) in the static HTML, just not exposed as JSON-LD."""

    m = VALENCIA_ES_EVENTS_RE.search(response_text)
    if not m:
        return []

    try:
        items = json.loads(m.group(1))
    except Exception:
        return []

    events = []
    for item in items:
        if not isinstance(item, dict):
            continue

        date = parse_date(item.get("startDateSort"))
        title = (item.get("content") or "").strip()
        if not date or not title:
            continue

        events.append({
            "title": title,
            "date": date,
            "url": VALENCIA_ES_CONTENT_BASE + str(item.get("url", "")),
            "description": item.get("description") or "",
        })

    return events


def from_rss(url):
    feed = feedparser.parse(url)
    events = []
    for entry in feed.entries:
        start = None
        for key in ("published", "updated", "pubDate"):
            if key in entry:
                start = parse_date(entry.get(key))
                if start:
                    break
        if not start:
            continue
        events.append({
            "title": entry.get("title", "").strip(),
            "date": start,
            "url": entry.get("link", url),
            "description": (entry.get("summary", "") or "")[:300],
        })
    return events


def find_feed_url(soup, base_url):
    link = soup.find("link", type="application/rss+xml")
    if link and link.get("href"):
        return urljoin(base_url, link["href"])
    return None


def find_ics_url(soup, base_url):
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".ics"):
            return urljoin(base_url, a["href"])
    return None


def from_ics(url):
    r = fetch(url)
    if not r:
        return []
    events = []
    try:
        cal = Calendar.from_ical(r.content)
    except Exception:
        return []
    for comp in cal.walk("VEVENT"):
        start = comp.get("dtstart")
        if not start:
            continue
        dt = start.dt
        if isinstance(dt, datetime):
            date = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        else:
            date = datetime(dt.year, dt.month, dt.day)
        events.append({
            "title": str(comp.get("summary", "")),
            "date": date,
            "url": str(comp.get("url", url)),
            "description": str(comp.get("description", ""))[:300],
        })
    return events


def from_tribe_events_api(response):
    """'The Events Calendar' WordPress plugin's public REST API — a common,
    actively-maintained source of real structured events (as opposed to the
    blog/news content most sites surface on their HTML agenda pages)."""
    try:
        data = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict) or "events" not in data:
        return []
    events = []
    for item in data["events"]:
        if not isinstance(item, dict):
            continue
        date = parse_date(item.get("start_date") or item.get("utc_start_date"))
        if not date:
            continue
        events.append({
            "title": (item.get("title") or "").strip(),
            "date": date,
            "url": item.get("url", ""),
            "description": re.sub(r"<[^>]+>", "", item.get("description") or "")[:300],
        })
    return events


DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)[a-z]*\.?\s*\d{0,4}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# Matches a plain HH:MM time (e.g. a showtime listed next to the date on an
# event card) so it can be merged into dates that otherwise parse as
# midnight — Spanish abbreviated-date formats like "24 jul." carry no time
# component of their own.
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


EVENT_CLASSES = (
    "event",
    "evento",
    "agenda",
    "calendar",
    "listing",
    "activity",
)

BAD_CLASSES = (
    "footer",
    "header",
    "cookie",
    "breadcrumb",
    "navbar",
    "navigation",
    "sidebar",
    "menu",
)

BAD_TITLES = {
    "read more",
    "leer más",
    "agenda",
    "home",
    "inicio",
    "contact",
    "contacto",
    "upcoming events",
    "past events",
    "events",
    "eventos",
}


def score_candidate(node):

    score = 0

    classes = " ".join(node.get("class", [])).lower()

    if any(c in classes for c in EVENT_CLASSES):
        score += 5

    if any(c in classes for c in BAD_CLASSES):
        score -= 10

    if node.find(["time"]):
        score += 4

    if node.find(["h1", "h2", "h3"]):
        score += 3

    if node.find("a", href=True):
        score += 2

    text = node.get_text(" ", strip=True)

    if DATE_RE.search(text):
        score += 5

    if len(text) < 40:
        score -= 3

    if len(text) > 500:
        score -= 2

    return score


# Some sites render a date badge (weekday + day/month, e.g. "Vie. 24 jul.")
# as a separate sibling element next to the real event card — an overlapping
# DOM node can pick up just that badge as its "title" instead of the actual
# event name. Reject titles that are just a date, or start with a bare one.
DATE_LIKE_TITLE_RE = re.compile(
    r"^(lun|mar|mi[eé]|jue|vie|s[aá]b|dom)\.?\s+\d{1,2}\s+"
    r"(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\.?\s*\d{0,4}\.?\s*$"
    r"|^\d{1,2}\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\b"
    r"|^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b",
    re.IGNORECASE,
)


def valid_title(title):

    title = title.strip()

    if len(title) < 8:
        return False

    if title.lower() in BAD_TITLES:
        return False

    if DATE_LIKE_TITLE_RE.match(title):
        return False

    return True


LANG_CLASSES = {"es", "va", "ca", "en", "fr", "de", "it", "pt", "gl", "eu"}
PREFERRED_LANGS = ("en", "es")


def clean_multilang(node):
    """Some sites render every language variant of text server-side and
    toggle visibility with CSS (e.g. <span class="notranslate es">...</span>
    <span class="notranslate en">...</span> side by side). A plain
    get_text() then concatenates every language into one garbled string.
    Detect that pattern and keep only one language's text."""

    lang_tagged = [
        el for el in node.find_all(True)
        if LANG_CLASSES & set(el.get("class") or [])
    ]

    if not lang_tagged:
        return node.get_text(" ", strip=True)

    present = set()
    for el in lang_tagged:
        present |= (LANG_CLASSES & set(el.get("class") or []))
    chosen = next((l for l in PREFERRED_LANGS if l in present), sorted(present)[0])

    parts = []
    for string in node.find_all(string=True):
        ancestor_langs = set()
        cur = string.parent
        while cur is not None and cur is not node.parent:
            ancestor_langs |= (LANG_CLASSES & set(cur.get("class") or []))
            cur = cur.parent
        if ancestor_langs and chosen not in ancestor_langs:
            continue
        s = string.strip()
        if s:
            parts.append(s)

    return " ".join(parts)


def from_html_heuristic(soup, base_url):

    events = []

    seen = set()
    claimed = set()

    candidates = soup.find_all(
        ["article", "section", "div", "li"],
        limit=5000,
    )

    scored = sorted(
        candidates,
        key=score_candidate,
        reverse=True,
    )

    for node in scored:

        if id(node) in claimed:
            continue

        if score_candidate(node) < 5:
            break

        text = clean_multilang(node)

        m = DATE_RE.search(text)

        if not m:
            continue

        date = parse_date(m.group())

        if not date:
            continue

        if date.hour == 0 and date.minute == 0:
            time_m = TIME_RE.search(text)
            if time_m:
                date = date.replace(hour=int(time_m.group(1)), minute=int(time_m.group(2)))

        heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])

        if heading:
            title = clean_multilang(heading)

        else:

            link = node.find("a")

            if link:
                title = clean_multilang(link)
            else:
                title = text[:120]

        if not valid_title(title):
            continue

        link = node.find("a", href=True)

        url = base_url

        if link:
            url = urljoin(base_url, link["href"])

        key = (
            title.lower(),
            date.date(),
        )

        if key in seen:
            continue

        seen.add(key)

        # Prevent any ancestor or descendant of this node from also being
        # picked up as a separate (duplicate/superset) candidate.
        claimed.add(id(node))
        for descendant in node.find_all(True):
            claimed.add(id(descendant))
        for ancestor in node.parents:
            claimed.add(id(ancestor))

        events.append({
            "title": title,
            "date": date,
            "url": url,
            "description": "",
        })

    return events


MAX_LLM_ARTICLES = 15


def extract_article_text(soup):
    """Grab body text from an article page to give the LLM context.

    Tries likely containers in priority order and uses the first one that
    actually has paragraph text — some sites' <article> tags are unrelated
    "related posts" teaser widgets with no body content at all, and the
    real content sits in a conventionally-named div (e.g. WordPress's
    "entry-content") instead.
    """
    candidates = (
        soup.find(class_="entry-content"),
        soup.find("article"),
        soup.find("main"),
    )
    for container in candidates:
        if container:
            paragraphs = container.find_all("p")
            if paragraphs:
                return " ".join(p.get_text(" ", strip=True) for p in paragraphs)

    return " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))


def find_article_candidates(soup, base_url):
    """Find distinct article title+link candidates from a listing page,
    reusing from_html_heuristic's card-scoring but WITHOUT requiring a date
    match — these sources' listing-page dates are unreliable (e.g. a
    "last updated" timestamp), so the actual date has to come from the LLM
    reading the individual article instead."""

    base_netloc = urlparse(base_url).netloc.removeprefix("www.")
    base_root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    candidates = soup.find_all(["article", "section", "div", "li"], limit=5000)
    scored = sorted(candidates, key=score_candidate, reverse=True)

    seen_urls = {base_url, base_root, base_root + "/"}
    claimed = set()
    results = []

    for node in scored:
        if id(node) in claimed:
            continue

        if score_candidate(node) < 5:
            break

        link = node.find("a", href=True)
        if not link:
            continue

        url = urljoin(base_url, link["href"])
        # Reject social-share buttons, off-site links, bare homepage/logo
        # links, and author/category archive pages — nested wrapper nodes
        # for the same card can pick up one of these (e.g. a byline link)
        # instead of the real article link.
        if (
            url in seen_urls
            or urlparse(url).netloc.removeprefix("www.") != base_netloc
            or re.search(r"/(author|category|tag)/", urlparse(url).path, re.IGNORECASE)
        ):
            continue

        heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        title = clean_multilang(heading) if heading else clean_multilang(link)

        if not valid_title(title):
            continue

        seen_urls.add(url)
        results.append({"title": title, "url": url})

        claimed.add(id(node))
        for descendant in node.find_all(True):
            claimed.add(id(descendant))
        for ancestor in node.parents:
            claimed.add(id(ancestor))

        if len(results) >= MAX_LLM_ARTICLES:
            break

    return results


def from_llm_assisted_articles(soup, base_url, today):
    """For prose-heavy guide-article sites: find candidate articles, fetch
    each one, and ask an LLM whether it describes a real dated event."""

    if not llm_extractor.available():
        return []

    events = []

    for cand in find_article_candidates(soup, base_url):
        r = fetch(cand["url"])
        if not r:
            continue

        try:
            article_soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            continue

        text = extract_article_text(article_soup)
        result = llm_extractor.extract_event(cand["title"], text, today)
        if not result:
            continue

        date = parse_date(result["start_date"])
        if not date:
            continue

        events.append({
            "title": result["title"],
            "date": date,
            "url": cand["url"],
            "description": text[:300],
        })

    return events


def from_llm_page_events(url, today):
    """For agenda pages with no per-card markup at all — just a plain-text
    listing behind a date/category filter widget — ask the LLM to pull
    every dated event out of the page text in one pass, rather than trying
    to discover per-item links to fetch individually."""

    if not llm_extractor.available():
        return []

    r = fetch(url)
    if not r:
        return []

    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        return []

    body = soup.find("body") or soup
    text = body.get_text(" ", strip=True)

    events = []
    for item in llm_extractor.extract_events_from_listing(text, today):
        date = parse_date(item["start_date"])
        if not date:
            continue
        events.append({
            "title": item["title"],
            "date": date,
            "url": url,
            "description": "",
        })

    return events


UPV_MONTH_RE = (
    r"(ene\w*|feb\w*|mar\w*|abr\w*|may\w*|jun\w*|jul\w*|ago\w*|"
    r"sep\w*|oct\w*|nov\w*|dic\w*)"
)


def _parse_upv_date_phrase(phrase):
    """UPV's date phrases are ranges like "del 15 de junio al 30 de
    septiembre de 2026" or single dates like "el 30 de septiembre de 2026"
    — dateutil's fuzzy parser can't reliably pick one date out of a range
    with two day/month pairs, so pull out the START day/month explicitly
    and pair it with whichever year appears in the phrase (ranges within
    the same year only state it once, at the end)."""

    year_m = re.search(r"\b(20\d{2})\b", phrase)
    day_month_m = re.search(rf"(\d{{1,2}})\s+de\s+{UPV_MONTH_RE}", phrase, re.IGNORECASE)

    if not year_m or not day_month_m:
        return None

    return parse_date(f"{day_month_m.group(1)} de {day_month_m.group(2)} de {year_m.group(1)}")


def from_upv_agenda(soup, base_url):
    """UPV's agenda page (upv.es/pls/oalu/sic_age.AgendaUPV) has a
    consistent per-event container (div.lista_eventos) with a real link,
    title, and "Inscripción"/"Realización" date labels — no LLM needed for
    this one, just a dedicated parser for its specific markup. "Realización"
    (when it actually happens) is preferred over "Inscripción" (just the
    registration window) as the event date."""

    events = []

    for container in soup.find_all("div", class_="lista_eventos"):

        link = container.find("a", class_="upv_enlace", href=True)
        if not link:
            continue

        title_tag = link.find(["strong", "b"]) or link
        title = clean_multilang(title_tag)
        if not valid_title(title):
            continue

        text = container.get_text(" ", strip=True)

        m = re.search(r"Realizaci[oó]n\s*(.+?)(?:$)", text, re.IGNORECASE)
        phrase = m.group(1) if m else None
        if not phrase:
            m = re.search(r"Inscripci[oó]n\s*(.+?)(?:Realizaci[oó]n|$)", text, re.IGNORECASE)
            phrase = m.group(1) if m else None
        if not phrase:
            continue

        date = _parse_upv_date_phrase(phrase)
        if not date:
            continue

        events.append({
            "title": title,
            "date": date,
            "url": urljoin(base_url, link["href"]),
            "description": "",
        })

    return events


def from_public_filtered(events):
    """Fetch each event's own page and drop anything the LLM judges to be
    restricted to students/staff rather than open to the general public.
    The listing page alone rarely states who an activity is for — that
    context is usually only in the individual event's own description."""

    if not llm_extractor.available():
        return events

    kept = []
    for event in events:
        r = fetch(event["url"])
        if not r:
            kept.append(event)  # can't check — don't drop over a fetch failure
            continue
        try:
            detail_soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            kept.append(event)
            continue

        detail_text = extract_article_text(detail_soup)
        if not detail_text:
            kept.append(event)
            continue

        if llm_extractor.check_open_to_public(event["title"], detail_text):
            kept.append(event)

    return kept


def _playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def discover_rendered_links(url, timeout_ms=30000):
    """Render `url` with a headless browser and return same-group/same-path
    links found in the fully-loaded DOM. Only for sites (e.g. Meetup) whose
    listing pages populate events via client-side JS after page load — a
    plain HTTP fetch gets an empty shell with no event data at all."""

    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # Some sites (e.g. Meetup) have continuous background network
                # chatter that never goes fully idle — the content we want
                # has usually already rendered well before this fires, so
                # just proceed with whatever's there rather than fail outright.
                log.info("networkidle wait timed out for %s, using current content", url)
            content = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(content, "lxml")
    base = urlparse(url)
    prefix = f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}"

    links = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("?")[0]
        # Restrict to the same group/listing path — e.g. Meetup renders
        # "similar events" from unrelated groups alongside this group's own,
        # which share the domain but not the path prefix.
        if href.startswith(prefix) and href != url.rstrip("/"):
            links.add(href)
    return links


def from_rendered_listing(url):
    """For JS-rendered listing pages: render once to discover this listing's
    own item links, then plain-fetch each individual page — those are
    typically server-rendered with real JSON-LD even when the listing
    itself isn't, so no further browser rendering is needed per item."""

    if not _playwright_available():
        log.warning("js_render requested but playwright isn't installed")
        return []

    events = []
    for link in discover_rendered_links(url):
        r = fetch(link)
        if not r:
            continue
        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            continue
        events.extend(from_jsonld(soup, link))

    return events


def discover_meetup_valencia_links(location="es--Valencia", max_scrolls=20):
    """Render Meetup's location-based 'find events' page (aggregates events
    from every group in the area, not just specific ones we've hand-picked)
    and scroll repeatedly to trigger its lazy-loading, then return one-off
    event links — recurring ones are dropped here using the page's own
    "Every ..." recurrence label on each card, which is a far more reliable
    signal than trying to infer recurrence from an event's title later."""

    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    url = f"https://www.meetup.com/find/?location={location}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                log.info("networkidle wait timed out for %s, using current content", url)
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1000)
            content = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(content, "lxml")
    # Each event card is this specific combination of utility classes —
    # found by inspecting the rendered page; the class names themselves are
    # generated (non-semantic) but the combination reliably scopes to one
    # card's own text, not its neighbors'.
    cards = soup.find_all(
        "div",
        class_=lambda c: c and "flex-col" in c and "overflow-hidden" in c and "active:scale-98" in c,
    )

    links = []
    for card in cards:
        link = card.find("a", href=lambda h: h and re.search(r"/events/\d+", h))
        if not link:
            continue

        text = card.get_text(" ", strip=True)

        if re.search(r"\bEvery\b", text):
            continue  # Meetup's own recurring-event label

        if re.search(r"Online\s+by\b", text, re.IGNORECASE):
            continue  # virtual/online event, not something to attend in Valencia

        if is_recurring(text):
            # Catches series an organizer reposts weekly by hand rather than
            # using Meetup's built-in recurring feature (no "Every" label),
            # when the title itself still gives it away (e.g. "Weekly ...",
            # a weekday-prefixed name). Cheap to check before even fetching
            # the individual event page.
            continue

        links.append(urljoin(url, link["href"]).split("?")[0])

    return list(dict.fromkeys(links))


def from_meetup_valencia_discovery():
    """Fetch every one-off event discovered across all Valencia Meetup
    groups. Individual event pages are typically server-rendered with real
    JSON-LD regardless of which group they belong to, so no further browser
    rendering is needed per event once the links are known."""

    if not _playwright_available():
        log.warning("meetup discovery requested but playwright isn't installed")
        return []

    events = []
    for link in discover_meetup_valencia_links():
        r = fetch(link)
        if not r:
            continue
        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            continue
        events.extend(from_jsonld(soup, link))

    return events


def scrape_source(source):
    """Returns list of raw event dicts for one source config."""
    url = source["url"]

    if source.get("meetup_discovery"):
        try:
            return from_meetup_valencia_discovery()
        except Exception:
            log.exception("%s: meetup_discovery failed", source["name"])
            return []

    if source.get("js_render"):
        # No point plain-fetching first — these listing pages are empty
        # shells without JS execution, so go straight to the headless path.
        try:
            return from_rendered_listing(url)
        except Exception:
            log.exception("%s: js_render failed", source["name"])
            return []

    if source.get("llm_extract_page"):
        try:
            return from_llm_page_events(url, datetime.now().date())
        except Exception:
            log.exception("%s: llm_extract_page failed", source["name"])
            return []

    if source.get("upv_agenda"):
        try:
            r = fetch(url)
            if not r:
                return []
            soup = BeautifulSoup(r.text, "lxml")
            events = from_upv_agenda(soup, url)
            if source.get("public_only"):
                events = from_public_filtered(events)
            return events
        except Exception:
            log.exception("%s: upv_agenda failed", source["name"])
            return []

    r = fetch(url)
    if not r:
        return []
    ctype = r.headers.get("Content-Type", "")

    def safe(fn, *args, label=""):

        try:
            result = fn(*args)

            if result:
                log.debug(
                    "%s parser succeeded (%d events)",
                    label,
                    len(result)
                )

            return result

        except ValueError as e:
    
            log.info(
                "%s parser rejected data (%s)",
                label,
                e
            )

        except KeyError as e:

            log.info(
                "%s missing field %s",
                label,
                e
             )

        except Exception:

            log.exception(
                "%s parser crashed",
                label
            )

    if "json" in ctype:
        events = safe(from_tribe_events_api, r, label="tribe_events")
        if events:
            return events
        return []

    if "xml" in ctype or url.endswith((".rss", ".xml")):
        events = safe(from_rss, url, label="rss")
        if events:
            return events

    events = safe(from_valencia_es_events, r.text, label="valencia_es")
    if events:
        return events

    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        log.warning("%s: lxml parse failed, falling back to html.parser", source["name"], exc_info=True)
        try:
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            log.warning("%s: html.parser also failed, giving up", source["name"], exc_info=True)
            return []

    if source.get("llm_extract") and llm_extractor.available():
        # These sources' listing-page dates are known-unreliable (guide
        # articles, not structured events) — trust the LLM path over the
        # generic heuristic rather than mixing wrong-dated results in.
        return safe(from_llm_assisted_articles, soup, url, datetime.now().date(), label="llm") or []

    events = safe(from_jsonld, soup, url, label="jsonld")
    if events:
        return events

    events = safe(from_next_data, soup, label="nextjs")

    if events:
        log.info("%-30s Next.js %3d events",
                 source["name"],
                 len(events))
        return events
   
    events = safe(from_nuxt_data, soup, label="nuxt")

    if events:
        log.info("%-30s Nuxt %3d events",
                 source["name"],
                 len(events))
        return events

    events = safe(from_embedded_json, soup, label="embedded_json")

    if events:
        log.info("%-30s Embedded JSON %3d events",
                 source["name"],
                 len(events))
        return events

    feed_url = safe(find_feed_url, soup, url, label="find_feed_url")
    if feed_url:
        events = safe(from_rss, feed_url, label="rss")
        if events:
            return events

    ics_url = safe(find_ics_url, soup, url, label="find_ics_url")
    if ics_url:
        events = safe(from_ics, ics_url, label="ics")
        if events:
            return events

    events = safe(
        from_html_heuristic,
        soup,
        url,
        label="html"
    )

    if events:
        log.info(
            "%-30s HTML %3d events",
            source["name"],
            len(events)
        )
    else:
        log.info(
            "%-30s no events",
            source["name"]
        )

    return events
