"""Weekly Valencia non-recurring events page. No paid AI usage (beyond an
optional free-tier LLM assist for a few prose-heavy sources)."""
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yaml
from dotenv import load_dotenv

load_dotenv()

from scraper import scrape_source
from classifier import tag_event, is_recurring, clean_title, drop_repeated_titles
from digest import build_digest, within_window

DEBUG = os.environ.get("DEBUG") == "1"
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                     format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

SOURCES_PATH = "src/sources.yaml"
MAX_WORKERS = 8

# The real deployment writes here — GitHub Pages serves this repo's /docs
# folder on the main branch. DRY_RUN writes to a separate file in the repo
# root instead, so local testing never touches the live page.
PAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "index.html")
DRY_RUN_PATH = os.path.join(os.path.dirname(__file__), "..", "digest_preview.html")


def load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def process_source(source):
    try:
        raw_events = scrape_source(source)
    except Exception as e:
        log.warning("error scraping %s", source["name"], exc_info=True)
        return []

    out = []
    for ev in raw_events:
        title = clean_title(ev.get("title", ""))
        date = ev.get("date")
        if not title or not date or not isinstance(date, datetime):
            continue
        desc = ev.get("description", "")
        if is_recurring(title, desc):
            continue
        out.append({
            "title": title,
            "date": date,
            "url": ev.get("url", source["url"]),
            "source": source["name"],
            "category": source["category"],
            "tags": tag_event(title, desc),
        })
    return drop_repeated_titles(out)


def main():
    sources = load_sources()
    all_events = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_source, s): s for s in sources}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                events = fut.result()
                log.info("%s: %d events", src["name"], len(events))
                if DEBUG:
                    for ev in events[:5]:
                        log.debug("  sample: %s | %s", ev["date"], ev["title"])
                all_events.extend(events)
            except Exception:
                log.warning("failed %s", src["name"], exc_info=True)

    # A hosted reference page should always show the full current picture —
    # not just what's new since the last run — so every event in the
    # window is included every time, with no seen/unseen tracking.
    windowed_events = [e for e in all_events if within_window(e["date"])]

    html, count = build_digest(windowed_events)

    out_path = DRY_RUN_PATH if os.environ.get("DRY_RUN") == "1" else PAGE_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    log.info("wrote %s (%d events)", out_path, count)


if __name__ == "__main__":
    sys.exit(main())
