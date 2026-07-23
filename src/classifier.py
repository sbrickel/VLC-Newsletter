"""Rule-based tagging, title cleanup and recurrence filtering.

Designed to be fast, deterministic and require no AI/API calls.
"""

from __future__ import annotations

import re
import unicodedata

# ----------------------------------------------------------------------
# Keyword dictionaries
# ----------------------------------------------------------------------

TAG_KEYWORDS = {
    "music": [
        "concert", "concierto", "musica", "música",
        "dj", "banda", "orquesta", "recital",
        "live", "festival"
    ],
    "art": [
        "arte", "art", "exhibicion", "exhibición",
        "exposicion", "exposición", "gallery",
        "galeria", "galería", "muestra"
    ],
    "theatre": [
        "teatro", "theatre", "obra",
        "danza", "dance", "performance"
    ],
    "film": [
        "cine", "film", "pelicula", "película",
        "screening", "documental"
    ],
    "talk": [
        "charla", "conferencia", "ponencia",
        "panel", "workshop", "taller",
        "lecture", "talk"
    ],
    "startup": [
        "startup", "networking",
        "pitch", "demo day",
        "hackathon", "meetup"
    ],
    "family": [
        "familia", "family",
        "infantil", "niños",
        "kids", "children"
    ],
    "food": [
        "gastronomia", "gastronomía",
        "food", "mercado", "market",
        "degustacion", "degustación",
        "wine", "vino", "beer", "cerveza"
    ],
    "culture_intl": [
        "consulado",
        "consulate",
        "instituto",
        "institut",
        "alliance",
        "cultural"
    ],
    "sports": [
        "sport", "sports",
        "deporte",
        "carrera",
        "race",
        "running",
        "torneo",
        "tournament"
    ],
}

# ----------------------------------------------------------------------
# Recurring phrases
# ----------------------------------------------------------------------

RECURRING_PATTERNS = [

    r"\bweekly\b",
    r"\bmonthly\b",
    r"\brecurring\b",
    r"\bdaily\b",

    r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",

    r"\bevery\s+week\b",
    r"\bevery\s+month\b",

    # Recurring meetups (language exchanges, socials, coworking sessions)
    # are conventionally named after their fixed weekday rather than saying
    # "weekly" or "every" explicitly, e.g. "Tuesday Language Exchange",
    # "Wednesday Language Exchange & Salsa/Bachata classes".
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",

    r"\bcada\s+(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)\b",

    r"\bcada\s+semana\b",
    r"\bcada\s+mes\b",

    r"\btodos?\s+los\b",

    r"\bclases?\s+semanales\b",

    r"\bcurso\s+semanal\b",

    r"\bopen\s+mic\b",

    r"\bjam\s+session\b",
]

BAD_TITLES = {
    "",
    "agenda",
    "eventos",
    "events",
    "home",
    "inicio",
    "leer más",
    "read more",
    "más información",
    "more info",
}

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def normalize(text: str) -> str:
    """
    Lowercase, remove accents and collapse whitespace.
    """

    text = unicodedata.normalize("NFKD", text or "")

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def clean_title(title: str) -> str:

    title = title or ""

    title = re.sub(r"\s+", " ", title)

    title = re.sub(r"[•·|]+", " - ", title)

    title = re.sub(r"\s+-\s+-", " - ", title)

    title = re.sub(r"\s{2,}", " ", title)

    title = title.strip(" -")

    return title[:140]


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def valid_title(title: str) -> bool:

    if not title:
        return False

    title = clean_title(title)

    if len(title) < 8:
        return False

    if normalize(title) in BAD_TITLES:
        return False

    return True


# ----------------------------------------------------------------------
# Tagging
# ----------------------------------------------------------------------


def tag_event(title: str, description: str = ""):

    text = normalize(f"{title} {description}")

    tags = []

    for tag, keywords in TAG_KEYWORDS.items():

        for keyword in keywords:

            if re.search(rf"\b{re.escape(normalize(keyword))}\b", text):

                tags.append(tag)

                break

    return tags or ["general"]


# ----------------------------------------------------------------------
# Recurrence detection
# ----------------------------------------------------------------------


def is_recurring(title: str, description: str = "") -> bool:

    text = normalize(f"{title} {description}")

    for pattern in RECURRING_PATTERNS:

        if re.search(pattern, text):

            return True

    return False


def drop_repeated_titles(events: list) -> list:
    """
    Drop events whose (source, normalized title) repeats across multiple
    distinct dates in this scrape — a strong signal of a recurring series
    even when the title has no explicit recurring marker (e.g. a generic
    "Free coworking session" posted every Monday). A title appearing only
    once, or on the same single date more than once, is left alone.
    """

    from collections import defaultdict

    groups = defaultdict(list)

    for ev in events:
        key = (ev.get("source"), normalize(ev.get("title", "")))
        groups[key].append(ev)

    recurring_keys = {
        key
        for key, group in groups.items()
        if len({ev["date"].date() for ev in group}) > 1
    }

    return [
        ev
        for ev in events
        if (ev.get("source"), normalize(ev.get("title", ""))) not in recurring_keys
    ]
