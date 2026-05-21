#!/usr/bin/env python3
"""Automated Corporate Event & Earnings iCalendar Generator.

Reads a CSV watchlist, fetches upcoming earnings dates (Finnhub primary,
yfinance fallback) and ex-dividend dates (yfinance), and writes a single
standards-compliant .ics file importable into Google / Apple / Outlook.

Usage:
    export FINNHUB_API_KEY=your_key_here          # macOS / Linux
    set FINNHUB_API_KEY=your_key_here             # Windows (cmd)
    python event_calendar.py --watchlist watchlist.csv --output watchlist.ics

Design notes:
  * Full regenerate every run -- no state file. Stable, date-independent
    UIDs let calendar clients update events instead of duplicating them.
  * Each ticker is isolated in try/except: one bad ticker never aborts the
    run. Missing data is logged loudly so you can fix the watchlist.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

from ics_writer import build_calendar
from providers import FinnhubEarningsProvider, ProviderError, YFinanceProvider


def load_watchlist(path: str) -> list[str]:
    """Read tickers from a CSV with a 'ticker' column. '#'-prefixed rows skipped."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "ticker" not in reader.fieldnames:
                sys.exit(f"ERROR: '{path}' must have a header row with a 'ticker' column.")
            tickers: list[str] = []
            for row in reader:
                t = (row.get("ticker") or "").strip().upper()
                if t and not t.startswith("#"):
                    tickers.append(t)
    except FileNotFoundError:
        sys.exit(f"ERROR: watchlist file not found: {path}")
    # De-duplicate while preserving order.
    seen: set[str] = set()
    return [t for t in tickers if not (t in seen or seen.add(t))]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--watchlist", default="watchlist.csv",
                        help="CSV file with a 'ticker' column (default: watchlist.csv)")
    parser.add_argument("--output", default="watchlist.ics",
                        help="output .ics path (default: watchlist.ics)")
    parser.add_argument("--lookahead", type=int, default=120,
                        help="days ahead to request earnings from Finnhub (default: 120)")
    parser.add_argument("--no-dividends", action="store_true",
                        help="skip ex-dividend events (earnings only)")
    parser.add_argument("--verbose", action="store_true", help="debug-level logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    log = logging.getLogger("event-calendar")

    tickers = load_watchlist(args.watchlist)
    if not tickers:
        sys.exit("ERROR: watchlist is empty.")
    log.info("Loaded %d ticker(s) from %s", len(tickers), args.watchlist)

    # Finnhub is optional: without a key the script falls back to yfinance
    # for earnings too (less reliable -- see README).
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    finnhub = None
    if api_key:
        finnhub = FinnhubEarningsProvider(api_key, lookahead_days=args.lookahead)
        log.info("Using Finnhub for earnings (yfinance for dividends/fallback).")
    else:
        log.warning("FINNHUB_API_KEY not set -- using yfinance for earnings too.")

    events_by_uid: dict[str, "object"] = {}   # uid -> Event ; dict dedups
    no_data: list[str] = []

    for ticker in tickers:
        collected = []

        # --- Earnings: Finnhub primary -------------------------------------
        got_earnings = False
        if finnhub is not None:
            try:
                ev = finnhub.fetch(ticker)
                collected += ev
                got_earnings = bool(ev)
                log.debug("%s: Finnhub returned %d earnings event(s)", ticker, len(ev))
            except ProviderError as e:
                log.warning("%s: Finnhub failed (%s)", ticker, e)

        # --- yfinance: fallback earnings + ex-dividend dates ---------------
        want_yf_earnings = not got_earnings
        want_yf_dividends = not args.no_dividends
        if want_yf_earnings or want_yf_dividends:
            yf = YFinanceProvider(want_earnings=want_yf_earnings,
                                  want_dividends=want_yf_dividends)
            try:
                collected += yf.fetch(ticker)
            except ProviderError as e:
                log.warning("%s: yfinance failed (%s)", ticker, e)

        if not collected:
            log.warning("%s: no upcoming events found "
                        "(delisted/renamed, or genuinely nothing scheduled).", ticker)
            no_data.append(ticker)
            continue

        # Last write wins. Providers run earnings-first, so a Finnhub
        # earnings event is never clobbered by a yfinance one.
        for ev in collected:
            events_by_uid[ev.uid] = ev
        log.info("%s: %d event(s)", ticker, len(collected))

    if not events_by_uid:
        sys.exit("ERROR: no events collected -- nothing to write.")

    ordered = sorted(events_by_uid.values(), key=lambda e: (e.date, e.uid))
    ics_text = build_calendar(ordered)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(ics_text)

    log.info("Wrote %d event(s) to %s", len(ordered), args.output)
    if no_data:
        log.warning("No data for %d ticker(s): %s -- review your watchlist.",
                    len(no_data), ", ".join(no_data))


if __name__ == "__main__":
    main()
