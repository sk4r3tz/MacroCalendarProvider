"""Standards-compliant iCalendar (.ics) writer -- standard library only.

Handles the three things hand-written .ics commonly gets wrong:
  1. CRLF line endings (RFC 5545 requires \\r\\n)
  2. Line folding at 75 octets, without splitting multi-byte UTF-8 chars
  3. TEXT-value escaping of  \\  ;  ,  and newlines

Output imports cleanly into Google Calendar, Apple Calendar and Outlook.
"""

from __future__ import annotations

import datetime as dt

from providers import Event

PRODID = "-//event-calendar-generator//Investment Watchlist//EN"
_EPOCH = dt.date(1970, 1, 1)


def _escape_text(text: str) -> str:
    """Escape a value used in a TEXT-typed property (SUMMARY, DESCRIPTION)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold one content line to <=75 octets per RFC 5545.

    Continuation lines begin with a single space. Folds on byte boundaries
    but never in the middle of a multi-byte UTF-8 character.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    chunks: list[bytes] = []
    while len(raw) > 75:
        cut = 75
        # Back off if `cut` lands on a UTF-8 continuation byte (10xxxxxx).
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(raw[:cut])
        raw = b" " + raw[cut:]   # continuation line -> leading space
    chunks.append(raw)
    return "\r\n".join(c.decode("utf-8") for c in chunks)


def _sequence(date: dt.date) -> int:
    """Stateless SEQUENCE number: days since the Unix epoch.

    A forward date shift raises SEQUENCE, so calendar clients honour the
    update. A backward shift will not raise it -- acceptable for a personal
    MVP (see README). For guaranteed updates in both directions, persist a
    uid -> sequence map and increment on every observed change.
    """
    return (date - _EPOCH).days


def build_calendar(events: list[Event],
                   calendar_name: str = "Investment Watchlist") -> str:
    """Render a list of Event objects into a complete VCALENDAR string."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text(calendar_name)}",
    ]

    for ev in events:
        start = ev.date.strftime("%Y%m%d")
        end = (ev.date + dt.timedelta(days=1)).strftime("%Y%m%d")  # all-day
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev.uid}",
            f"DTSTAMP:{now}",
            f"SEQUENCE:{_sequence(ev.date)}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{_escape_text(ev.title)}",
            f"DESCRIPTION:{_escape_text(ev.description)}",
            "TRANSP:TRANSPARENT",   # event does not block free/busy time
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
