# Valencia Non-Recurring Events Page

Weekly GitHub Action. Scrapes ~55 sources, tags events, and rebuilds a
static page of the next 14 days, hosted via GitHub Pages from this repo's
`docs/` folder. No paid AI/API usage anywhere (an optional free-tier LLM
assists a handful of prose-heavy sources — see below).

## How it works
1. `src/sources.yaml` — one entry per source (name, URL, category).
2. `src/scraper.py` — per source, tries in order: JSON-LD `Event` schema,
   RSS/Atom feed, `.ics` calendar link, then a generic HTML heuristic
   (date-regex + nearest heading/link), plus a few dedicated strategies
   for specific sources (Meetup's location-wide event discovery via a
   headless browser, UPV's agenda markup, valencia.es's embedded JSON).
   Whichever succeeds first wins.
3. `src/classifier.py` — keyword rules tag events (music, art, talk,
   startup, etc.) and drop anything phrased as recurring/weekly.
4. `src/digest.py` — filters to the next 14 days, builds the page (a
   month calendar plus a date-sorted event list), and stamps it with a
   "last updated" time — the page only regenerates weekly, so it always
   shows the *full* current 14-day window, not just what's new.
5. `main.py` writes the result to `docs/index.html`, which GitHub Pages
   serves directly — the Action commits it back to the repo each run.

## Setup
1. Push this repo to GitHub.
2. In the repo's **Settings → Pages**, set Source to "Deploy from a
   branch", branch `main`, folder `/docs`. (One-time step.)
3. Optional: add a `GROQ_API_KEY` repo secret (see below) for LLM-assisted
   sources.
4. Enable Actions. It runs Mondays 07:00 UTC, or trigger manually via
   "Run workflow". The page will be live at
   `https://<your-username>.github.io/<repo-name>/`.

## Known limitation — read before relying on it
This pass fixed:
- **403s** (CaixaForum, Bombas Gens, 24/7 Valencia, Lions Club): real browser
  headers (Chrome UA, Accept-Language) now sent by default via a shared
  `requests.Session`.
- **500/502/503/504**: automatic retry with backoff (2 attempts) on the
  session.
- **Wrong URLs** (404/DNS failures): corrected for Visit Valencia,
  Bombas Gens, Palau de la Música, Museo de Bellas Artes, La Marina de
  València (was `marinavalencia.com`, real domain is
  `lamarinadevalencia.com` — fixes Marina/Veles e Vents/Tinglado 2&4/
  Marina Norte too), Startup Valencia (was `.es`, real domain `.org`),
  and Valencia Digital Summit (real domain `vds.tech`).
- **Silent crashes** (e.g. the Edem "not enough values to unpack" error):
  each extraction strategy (JSON-LD/RSS/ICS/heuristic) is now isolated
  in its own try/except with a full traceback logged, so one bad parse
  no longer kills that source's results, and you can see *why* next time.

Still unresolved — needs a follow-up pass, since these could not be
verified from this environment (network access here is restricted to
package registries, not arbitrary sites):
- Several sites (Time Out Valencia, Matisse Club, Fabrica de Hielo,
  Mercat del Cabanyal, Base One, Mercat del Grau, Palauet d'Ayora,
  Universidad Popular Ayora, Centro Cultural Aben Al-Abbar, ESIC,
  UPV agenda) return 404 — the agenda page has likely moved or never
  existed at that path; each needs its correct URL looked up.
- DNS failures (La Mutant, Alliance Française, Dante Alighieri,
  Instituto Confucio, Intercultural Spain, several consulates) suggest
  the guessed domain is wrong or the org has no standalone website —
  worth checking their Facebook/Instagram instead.
- Generalitat Valenciana Cultura has no clean HTML agenda; use their
  open-data feed instead: https://dadesobertes.gva.es/es/dataset/cul-agc-ivc-hist
  (structured XML, would need a small dedicated parser).
- Even where a source loads correctly, the generic HTML heuristic can
  mis-parse dates on JS-heavy or unusually structured pages — run with
  `DEBUG=1` and check the sampled title/date pairs before trusting a
  source's output.

## Local test
```
pip install -r requirements.txt
playwright install chromium   # needed once, for JS-rendered sources (Meetup)
DRY_RUN=1 python src/main.py
```
`DRY_RUN=1` writes to `digest_preview.html` in the repo root instead of
`docs/index.html`, so local runs never touch the live page. Without it,
`python src/main.py` writes directly to `docs/index.html` — same as the
real deployment.

If Chromium fails to launch with a `libnspr4.so` (or similar) error, your
environment is missing its system libraries. Normally `playwright install
--with-deps chromium` installs these via sudo, but if you don't have root
locally, run `scripts/setup_playwright_libs.sh` instead — it downloads and
extracts the same packages into `.playwright-libs/` without installing them
system-wide. `scraper.py` picks this up automatically; no environment
variables to set by hand.

Optional: set `GROQ_API_KEY` (in a local `.env` file, or exported) to enable
LLM-assisted date extraction for prose-heavy guide-article sources (Visit
Valencia, Valenciabonita) — free tier at https://console.groq.com, no card
required. Without it, those two sources fall back to the generic HTML
heuristic, which for these particular sites produces unreliable dates (the
real date is only in body prose, not any structured field).
