"""Event data providers.

Provider-agnostic design: every provider exposes `fetch(ticker) -> list[Event]`.
Add a new source (e.g. SEC EDGAR, an economic-calendar feed) by writing one
more class with the same `fetch` signature -- nothing else needs to change.

Standard library only, except YFinanceProvider which imports `yfinance`
lazily (so the script still runs with Finnhub alone if yfinance is absent).
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


# --------------------------------------------------------------------------
# Shared model
# --------------------------------------------------------------------------
@dataclass
class Event:
    """One calendar event.

    `uid` MUST be stable and date-independent: it identifies the *logical*
    event (e.g. "AAPL Q3 2026 earnings"), not the date. When a company shifts
    its earnings date, the same uid re-emits with a new `date`, so calendar
    clients update the existing entry instead of creating a duplicate.
    """
    uid: str          # stable identity -> drives dedup / update behaviour
    title: str        # -> SUMMARY
    date: dt.date     # -> DTSTART (all-day)
    description: str  # -> DESCRIPTION
    source: str       # which provider produced it (for logging)


class ProviderError(Exception):
    """Raised for any recoverable per-ticker provider failure."""


# --------------------------------------------------------------------------
# Date parsing -- tolerant of every shape these APIs throw at us
# --------------------------------------------------------------------------
def parse_date(value) -> dt.date | None:
    """Return a date, or None for missing / NaN / NaT / unparseable input."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    # pandas Timestamp (and NaT) expose .date(); NaT raises -> treated as None
    if hasattr(value, "date") and callable(getattr(value, "date")):
        try:
            return value.date()
        except Exception:
            return None
    if isinstance(value, (int, float)):
        if value != value:          # NaN
            return None
        try:
            return dt.datetime.utcfromtimestamp(float(value)).date()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("nan", "nat", "none", "null"):
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None
    return None


def _calendar_quarter(d: dt.date) -> int:
    return (d.month - 1) // 3 + 1


# --------------------------------------------------------------------------
# Finnhub -- primary earnings source (official API, real forward calendar)
# --------------------------------------------------------------------------
class FinnhubEarningsProvider:
    """Earnings dates via Finnhub's /calendar/earnings endpoint.

    Free tier: 60 requests/minute. One request per ticker here -- fine for a
    personal watchlist of tens of tickers. For a large list you could fetch
    the whole calendar in one call (omit `symbol`) and filter locally.
    """

    name = "finnhub"
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, lookahead_days: int = 120, timeout: int = 15):
        if not api_key:
            raise ProviderError("Finnhub API key missing")
        self.api_key = api_key
        self.lookahead_days = lookahead_days
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = dict(params, token=self.api_key)
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "event-calendar-generator/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise ProviderError("Finnhub rate limit (HTTP 429)")
            if e.code in (401, 403):
                raise ProviderError(f"Finnhub auth error (HTTP {e.code}) -- check API key")
            raise ProviderError(f"Finnhub HTTP {e.code}")
        except urllib.error.URLError as e:
            raise ProviderError(f"Finnhub network error: {e.reason}")
        except json.JSONDecodeError:
            raise ProviderError("Finnhub returned non-JSON response")

    def fetch(self, ticker: str) -> list[Event]:
        today = dt.date.today()
        end = today + dt.timedelta(days=self.lookahead_days)
        data = self._get(
            "/calendar/earnings",
            {"from": today.isoformat(), "to": end.isoformat(), "symbol": ticker},
        )
        rows = data.get("earningsCalendar") or []
        events: list[Event] = []
        for row in rows:
            d = parse_date(row.get("date"))
            if d is None or d < today:
                continue

            year = row.get("year")
            quarter = row.get("quarter")
            if not year or not quarter:
                # Fall back to calendar quarter so the UID is still stable.
                year, quarter = d.year, _calendar_quarter(d)

            uid = f"earnings-{ticker.upper()}-{year}Q{quarter}@event-calendar"

            hour = (row.get("hour") or "").lower()
            timing = {
                "bmo": "Before market open",
                "amc": "After market close",
                "dmh": "During market hours",
            }.get(hour, "Time of day TBD")

            # Treat dates within 2 weeks as effectively confirmed.
            confirmed = (d - today) <= dt.timedelta(days=14)
            tag = "" if confirmed else " (estimated)"
            title = f"{ticker.upper()} \u2014 Q{quarter} {year} Earnings{tag}"

            desc = [
                f"{ticker.upper()} earnings release \u2014 Q{quarter} {year}",
                f"Timing: {timing}",
            ]
            eps = row.get("epsEstimate")
            rev = row.get("revenueEstimate")
            if eps is not None:
                desc.append(f"EPS estimate: {eps}")
            if rev:
                desc.append(f"Revenue estimate: {rev:,}")
            desc.append(
                "Date confirmed." if confirmed
                else "Date is an estimate and may shift by a few days."
            )
            desc.append(f"Source: Finnhub. Pulled {today.isoformat()}.")
            desc.append(f"https://finance.yahoo.com/quote/{ticker.upper()}")

            events.append(Event(uid, title, d, "\n".join(desc), self.name))
        return events


# --------------------------------------------------------------------------
# yfinance -- fallback earnings + primary ex-dividend source
# --------------------------------------------------------------------------
class YFinanceProvider:
    """Earnings fallback and ex-dividend dates via the `yfinance` library.

    yfinance scrapes Yahoo Finance: unofficial, rate-limited, and fragile.
    It is used here only to (a) fill earnings gaps Finnhub misses and
    (b) supply ex-dividend dates, which the Finnhub free tier does not give.
    """

    name = "yfinance"

    def __init__(self, want_earnings: bool = True, want_dividends: bool = True,
                 sleep_seconds: float = 2.0):
        self.want_earnings = want_earnings
        self.want_dividends = want_dividends
        self.sleep_seconds = sleep_seconds  # politeness delay -> avoids 429s

    def fetch(self, ticker: str) -> list[Event]:
        try:
            import yfinance as yf
        except ImportError:
            raise ProviderError("yfinance not installed (pip install yfinance)")

        time.sleep(self.sleep_seconds)
        try:
            cal = yf.Ticker(ticker).calendar or {}
        except Exception as e:  # yfinance raises a wide variety of errors
            raise ProviderError(f"yfinance error for {ticker}: {e}")

        today = dt.date.today()
        events: list[Event] = []

        if self.want_earnings:
            raw = cal.get("Earnings Date")
            # May be a single date or a two-element range; take the earliest.
            candidates = raw if isinstance(raw, (list, tuple)) else [raw]
            for item in candidates:
                d = parse_date(item)
                if d is None or d < today:
                    continue
                q = _calendar_quarter(d)
                uid = f"earnings-{ticker.upper()}-{d.year}Q{q}@event-calendar"
                events.append(Event(
                    uid,
                    f"{ticker.upper()} \u2014 Q{q} {d.year} Earnings (estimated)",
                    d,
                    f"{ticker.upper()} earnings release (approximate date).\n"
                    f"Date is an estimate and may shift.\n"
                    f"Source: yfinance / Yahoo Finance. Pulled {today.isoformat()}.\n"
                    f"https://finance.yahoo.com/quote/{ticker.upper()}",
                    self.name,
                ))
                break  # yfinance reliably gives at most the next date

        if self.want_dividends:
            exd = parse_date(cal.get("Ex-Dividend Date"))
            if exd and exd >= today:
                q = _calendar_quarter(exd)
                uid = f"dividend-{ticker.upper()}-{exd.year}Q{q}@event-calendar"
                events.append(Event(
                    uid,
                    f"{ticker.upper()} \u2014 Ex-Dividend Date",
                    exd,
                    f"{ticker.upper()} ex-dividend date.\n"
                    f"Own the stock before this date to receive the dividend.\n"
                    f"Source: yfinance / Yahoo Finance. Pulled {today.isoformat()}.",
                    self.name,
                ))

        return events
