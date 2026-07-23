"""Builds HTML and plaintext newsletter digests."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _VALENCIA_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    _VALENCIA_TZ = None

# ----------------------------------------------------------------------
# Design tokens — Valencia-inspired palette (warm orange, Mediterranean
# blue, light sand). Kept as plain inline styles throughout (no <style>
# block, no flexbox/grid) since that's the only styling approach that
# renders consistently across email clients, including Outlook desktop.
# ----------------------------------------------------------------------

COLOR_BG = "#F6EFE1"
COLOR_CARD = "#FFFFFF"
COLOR_TEXT = "#332E27"
COLOR_MUTED = "#8B8171"
COLOR_DIVIDER = "#EAE0CC"
COLOR_ORANGE = "#E2692F"
COLOR_ORANGE_TINT = "#FBE9DD"
COLOR_BLUE = "#1E6E8C"
COLOR_BLUE_TINT = "#E3F0F3"
COLOR_DIM_BG = "#FBF8F1"
COLOR_DIM_TEXT = "#C9BFA9"

FONT_SERIF = "Georgia, 'Times New Roman', serif"
FONT_SANS = "Helvetica, Arial, sans-serif"

# ----------------------------------------------------------------------
# Category names
# ----------------------------------------------------------------------

CATEGORY_LABELS = {
    "official": "🏛 Official & City",
    "magazine": "📰 Local Magazines",
    "museum": "🖼 Museums & Institutions",
    "music": "🎵 Music & Performance",
    "cabanyal": "🌊 Cabanyal / Canyamelar",
    "grau": "⚓ El Grau / Marina",
    "ayora": "🌳 Ayora / Camins al Grau",
    "university": "🎓 Universities",
    "consulate": "🌍 Consulates & Cultural Associations",
    "startup": "🚀 Startup & Professional",
    "community": "🤝 Community",
}

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def normalize_datetime(dt: datetime) -> datetime:
    """Convert timezone-aware datetimes to naive ones."""

    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)

    return dt


def within_window(date: datetime, days: int = 14) -> bool:
    """
    Whether `date` falls within the next `days` days, inclusive of today.

    Compares calendar dates rather than exact timestamps: most scraped
    events have no real time-of-day and default to midnight, so comparing
    against datetime.now() (which has a nonzero time) would wrongly exclude
    every "today" event as soon as any time has passed since midnight.
    """

    date = normalize_datetime(date)

    today = datetime.now().date()

    return today <= date.date() <= today + timedelta(days=days)


def format_date(dt: datetime) -> str:

    if dt.hour == 0 and dt.minute == 0:
        return dt.strftime("%a %d %b")

    return dt.strftime("%a %d %b • %H:%M")


def format_generated_at() -> str:
    """The page only regenerates once a week, so it needs to say when it
    was last built — otherwise a visitor has no way to tell a stale-looking
    page from a genuinely quiet week."""

    now_utc = datetime.now(timezone.utc)

    if _VALENCIA_TZ:
        local = now_utc.astimezone(_VALENCIA_TZ)
        return local.strftime("%A, %d %b %Y at %H:%M") + " (Valencia time)"

    return now_utc.strftime("%A, %d %b %Y at %H:%M") + " UTC"


# ----------------------------------------------------------------------
# Page shell — opens/closes the outer email wrapper and rounded card
# ----------------------------------------------------------------------

def html_header(count: int) -> list[str]:

    return [
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        # Without these, Gmail/Apple Mail's automatic dark-mode processing
        # can invert this palette's intentional light backgrounds — this
        # opts the message out so the colors render as designed.
        "<meta name='color-scheme' content='light'>",
        "<meta name='supported-color-schemes' content='light'>",
        "</head>",
        f"<body style='margin:0;padding:0;background:{COLOR_BG};'>",
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='background:{COLOR_BG};'>",
        "<tr><td align='center' style='padding:24px 12px;'>",
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='max-width:600px;background:{COLOR_CARD};border-radius:16px;"
        f"overflow:hidden;font-family:{FONT_SANS};'>",

        "<tr><td style='padding:30px 28px 18px 28px;'>",
        f"<div style='width:44px;height:4px;background:{COLOR_ORANGE};"
        "border-radius:2px;margin-bottom:16px;'></div>",
        f"<h1 style=\"margin:0 0 8px 0;font-family:{FONT_SERIF};"
        f"font-size:26px;font-weight:bold;color:{COLOR_TEXT};\">"
        "Valencia Events</h1>",
        f"<p style='margin:0 0 10px 0;font-size:14px;color:{COLOR_MUTED};'>",
        f"<span style='color:{COLOR_ORANGE};font-weight:bold;'>{count}</span> "
        "upcoming non-recurring events in the next 14 days",
        "</p>",
        # This page only regenerates weekly, so it needs to say when —
        # otherwise a visitor has no way to tell a stale page from a
        # genuinely quiet week.
        f"<span style='display:inline-block;background:{COLOR_BLUE_TINT};"
        f"color:{COLOR_BLUE};font-size:11px;font-weight:bold;padding:4px 10px;"
        f"border-radius:10px;'>Last updated: {format_generated_at()}</span>",
        "</td></tr>",
    ]


def html_footer() -> list[str]:

    return [
        "<tr><td style='padding:8px 28px 30px 28px;'>",
        f"<div style='border-top:1px solid {COLOR_DIVIDER};padding-top:16px;'>",
        f"<p style='margin:0;font-size:12px;color:{COLOR_MUTED};'>",
        "Generated automatically.",
        "</p>",
        "</div>",
        "</td></tr>",

        "</table>",  # inner card
        "</td></tr>",
        "</table>",  # outer wrapper
        "</body>",
        "</html>",
    ]


# ----------------------------------------------------------------------
# Month-grid calendar
# ----------------------------------------------------------------------

def build_calendar_grid(events, today, window_end):
    """
    HTML month-grid calendar (table-based: email clients like Outlook don't
    reliably support CSS grid/flexbox, but table layouts work everywhere).

    Spans full calendar weeks from the Monday of `today`'s week through the
    Sunday of `window_end`'s week, so the grid always shows complete rows.
    Days outside [today, window_end] are rendered dimmed with no events.
    """

    events_by_day = defaultdict(list)
    for e in events:
        events_by_day[e["date"].date()].append(e)

    grid_start = today - timedelta(days=today.weekday())
    grid_end = window_end + timedelta(days=(6 - window_end.weekday()))

    html = [
        "<tr><td style='padding:4px 28px 6px 28px;'>",
        f"<table role='presentation' cellpadding='0' cellspacing='0' width='100%' "
        f"style='border-collapse:separate;border-spacing:0;table-layout:fixed;"
        f"border:1px solid {COLOR_DIVIDER};border-radius:10px;overflow:hidden;'>",
        "<tr>",
    ]

    for i, name in enumerate(WEEKDAY_NAMES):
        corner = ""
        if i == 0:
            corner = "border-top-left-radius:9px;"
        elif i == len(WEEKDAY_NAMES) - 1:
            corner = "border-top-right-radius:9px;"
        html.append(
            f"<th style='font-size:11px;color:#FFFFFF;background:{COLOR_BLUE};"
            f"padding:7px 4px;text-align:left;font-weight:600;{corner}'>{name[:3]}</th>"
        )
    html.append("</tr>")

    day = grid_start
    while day <= grid_end:
        html.append("<tr>")
        for _ in range(7):
            in_range = today <= day <= window_end
            is_today = day == today
            day_events = events_by_day.get(day, [])

            cell_style = (
                f"vertical-align:top;padding:6px;border-top:1px solid {COLOR_DIVIDER};"
                f"height:76px;font-size:11px;background:{COLOR_CARD};"
            )
            if not in_range:
                cell_style += f"background:{COLOR_DIM_BG};color:{COLOR_DIM_TEXT};"

            month_prefix = day.strftime("%b ") if day.day == 1 else ""
            day_anchor = f"day-{day.isoformat()}"

            html.append(f"<td style='{cell_style}'>")

            if is_today:
                day_label = (
                    f"<span style='display:inline-block;min-width:18px;height:18px;"
                    f"line-height:18px;text-align:center;border-radius:50%;"
                    f"background:{COLOR_ORANGE};color:#FFFFFF;font-weight:bold;"
                    f"font-size:11px;padding:0 2px;'>{month_prefix}{day.day}</span>"
                )
            else:
                text_color = COLOR_DIM_TEXT if not in_range else COLOR_TEXT
                day_label = f"<span style='color:{text_color};'>{month_prefix}{day.day}</span>"

            if in_range and day_events:
                html.append(
                    f"<a href='#{day_anchor}' style='text-decoration:none;'>{day_label}</a>"
                )
            else:
                html.append(f"<div>{day_label}</div>")

            if in_range and day_events:
                shown = day_events[:2]
                for ev in shown:
                    html.append(
                        f"<div style='color:{COLOR_BLUE};overflow:hidden;"
                        "text-overflow:ellipsis;white-space:nowrap;margin-top:3px;'>"
                        f"● {ev['title'][:16]}</div>"
                    )
                extra = len(day_events) - len(shown)
                if extra > 0:
                    # Real email clients can't run JS to expand/collapse content,
                    # so "+N more" is a same-page link down to that day's full
                    # listing in the detail section below, not a live toggle.
                    html.append(
                        f"<a href='#{day_anchor}' style='color:{COLOR_ORANGE};"
                        "font-size:10px;font-weight:bold;text-decoration:none;'>"
                        f"+{extra} more</a>"
                    )

            html.append("</td>")
            day += timedelta(days=1)
        html.append("</tr>")

    html.append("</table>")
    html.append("</td></tr>")
    return html


# ----------------------------------------------------------------------
# Main builder
# ----------------------------------------------------------------------

def build_digest(events):
    """
    Build the HTML page.

    Returns

        html,
        event_count
    """

    normalized = []

    for event in events:

        event = event.copy()

        event["date"] = normalize_datetime(event["date"])

        normalized.append(event)

    upcoming = [
        e
        for e in normalized
        if within_window(e["date"])
    ]

    upcoming.sort(
        key=lambda e: (
            e["date"],
            e["title"].lower(),
        )
    )

    grouped_by_day = defaultdict(list)

    for event in upcoming:
        grouped_by_day[event["date"].date()].append(event)

    today = datetime.now().date()
    window_end = (datetime.now() + timedelta(days=14)).date()

    html = html_header(len(upcoming))
    html.extend(build_calendar_grid(upcoming, today, window_end))

    html.append(f"<tr><td style='padding:10px 28px 4px 28px;'>")

    # Grouped by day (not category) so the calendar's day links land on a
    # single section containing every event for that date, sorted by date —
    # matching upcoming's own sort order since grouped_by_day preserves it.
    for day in sorted(grouped_by_day):

        items = grouped_by_day[day]

        html.append(
            f"<div id='day-{day.isoformat()}' style='margin:18px 0 10px 0;"
            f"padding-bottom:6px;border-bottom:2px solid {COLOR_ORANGE};'>"
            f"<span style=\"font-family:{FONT_SERIF};font-size:16px;"
            f"font-weight:bold;color:{COLOR_TEXT};\">"
            f"{WEEKDAY_NAMES[day.weekday()]}, {day.strftime('%d %b')}"
            "</span></div>"
        )

        for event in items:

            category_label = CATEGORY_LABELS.get(event["category"], event["category"])

            has_time = not (event["date"].hour == 0 and event["date"].minute == 0)
            time_badge = event["date"].strftime("%H:%M") if has_time else "All day"

            html.append(
                f"<div style='background:{COLOR_CARD};border:1px solid {COLOR_DIVIDER};"
                "border-radius:12px;padding:14px 16px;margin-bottom:10px;"
                "box-shadow:0 1px 3px rgba(51,46,39,0.06);'>"
                "<table role='presentation' cellpadding='0' cellspacing='0' width='100%'><tr>"
                "<td style='vertical-align:top;width:60px;padding-right:12px;'>"
                f"<div style='background:{COLOR_ORANGE_TINT};color:{COLOR_ORANGE};"
                "font-size:11px;font-weight:bold;border-radius:8px;padding:5px 2px;"
                f"text-align:center;white-space:nowrap;'>{time_badge}</div>"
                "</td>"
                "<td style='vertical-align:top;'>"
                f"<a href='{event['url']}' target='_blank' rel='noopener' "
                f"style=\"color:{COLOR_BLUE};font-size:15px;font-weight:bold;"
                f"text-decoration:none;font-family:{FONT_SERIF};\">"
                f"{event['title']}</a><br>"
                f"<span style='display:inline-block;margin-top:7px;"
                f"background:{COLOR_BLUE_TINT};color:{COLOR_BLUE};font-size:11px;"
                "padding:3px 9px;border-radius:10px;'>"
                f"{category_label}</span> "
                f"<span style='font-size:12px;color:{COLOR_MUTED};'>{event['source']}</span>"
                "</td>"
                "</tr></table>"
                "</div>"
            )

    if not upcoming:

        html.append(
            f"<p style='color:{COLOR_MUTED};'><b>No upcoming events found "
            "for the next 14 days.</b></p>"
        )

    html.append("</td></tr>")

    html.extend(html_footer())

    return (
        "\n".join(html),
        len(upcoming),
    )
