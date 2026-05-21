# Corporate Event & Earnings iCalendar Generator

Generates a single `.ics` file of upcoming **earnings dates** and
**ex-dividend dates** for a personal investment watchlist. Importable into
Google Calendar, Apple Calendar, and Outlook.

## Files

| File                 | Purpose                                                |
|----------------------|--------------------------------------------------------|
| `event_calendar.py`  | Main script — run this.                                |
| `providers.py`       | Data layer: `Event` model + Finnhub & yfinance providers. |
| `ics_writer.py`      | RFC 5545 `.ics` writer (standard library only).        |
| `watchlist.csv`      | Your input — one ticker per row.                       |

## Setup

1. **Python 3.9+** (uses `zoneinfo`-era stdlib; no `zoneinfo` import, but 3.9+ assumed).
2. Install yfinance (used for ex-dividend dates and as an earnings fallback):
   ```
   pip install yfinance
   ```
   Nothing else is needed — Finnhub access uses only `urllib`.
3. Get a free Finnhub API key at <https://finnhub.io> and set it:
   ```
   export FINNHUB_API_KEY=your_key_here     # macOS / Linux
   set FINNHUB_API_KEY=your_key_here        # Windows (cmd)
   $env:FINNHUB_API_KEY="your_key_here"     # Windows (PowerShell)
   ```

## Usage

```
python event_calendar.py --watchlist watchlist.csv --output watchlist.ics
```

Options: `--lookahead DAYS` (default 120), `--no-dividends`, `--verbose`.

Edit `watchlist.csv` to change tickers — it needs a header row with a
`ticker` column; rows whose ticker starts with `#` are ignored.

## Importing the result

- **Apple Calendar / Outlook desktop:** File → Import, pick `watchlist.ics`.
- **Google Calendar:** Settings → Import & export → Import.

Re-running overwrites `watchlist.ics`. Because every event carries a stable,
date-independent `UID`, re-importing **updates** existing events (including
moved earnings dates) rather than duplicating them.

## Scheduling (run once daily)

Daily is plenty — earnings data does not change faster than that, and more
frequent calls raise the risk of a Yahoo rate-limit block.

**macOS / Linux (cron):** `crontab -e`, then:
```
0 7 * * *  cd /path/to/folder && FINNHUB_API_KEY=your_key /usr/bin/python3 event_calendar.py
```

**Windows (Task Scheduler):** Create Task → daily trigger → Action: start
`python.exe` with argument `event_calendar.py`, "Start in" set to this folder.
Set `FINNHUB_API_KEY` as a user environment variable first.

## How it works

- **Earnings:** Finnhub `/calendar/earnings` (real forward calendar, EPS
  estimate, pre/post-market timing). If Finnhub returns nothing for a ticker,
  yfinance fills in an approximate date.
- **Ex-dividend dates:** yfinance `Ticker.calendar` (Finnhub's free tier
  does not include dividend dates).
- **Resilience:** each ticker is isolated in `try/except`; one failure never
  aborts the run. Tickers with no data are logged at the end so you can fix
  the watchlist (e.g. a renamed or delisted symbol).

## Known limitations

- **Estimated dates shift.** Earnings dates more than ~2 weeks out are
  estimates; they are labelled "(estimated)" in the event title and may move.
  Re-running daily propagates corrections.
- **`SEQUENCE` is stateless** — derived from the event date (days since
  epoch). A *forward* date shift raises `SEQUENCE` so clients honour the
  update; a *backward* shift will not. To guarantee updates in both
  directions, persist a `uid -> sequence` map and increment on every change.
- **yfinance can be rate-limited** (HTTP 429) if Yahoo sees too many
  requests. The script sleeps ~2s between yfinance calls; keep the watchlist
  modest and run no more than once a day. Run from a residential IP — cloud
  / datacenter IPs get blocked faster.
- **No conference-call dial-ins or investor-day events** — neither data
  source exposes them reliably.

## Extending it

Add a new event type by writing one more provider class in `providers.py`
with a `fetch(ticker) -> list[Event]` method, then calling it in
`event_calendar.py`. Good next sources: Finnhub's economic-calendar endpoint
(Fed / CPI dates) and SEC EDGAR `data.sec.gov` (10-Q filing download).
