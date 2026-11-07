#!/usr/bin/env python3
"""
otc_daily.py - Download daily candles (OHLCV) for all US OTC stocks from
Massive (formerly Polygon.io), using the free "Stocks Basic" plan.

How it works
  - One API call = one trading day for the WHOLE market (grouped daily endpoint
    with include_otc=true). The script keeps only OTC tickers by default.
  - Results go into a local SQLite database (otc_daily.db). Re-running the
    script resumes where it stopped and only fetches days it doesn't have yet,
    so the same command works for the first backfill and for daily updates.
  - Days are downloaded newest first, so recent weeks arrive within minutes.
  - The free plan allows ~5 calls/minute, so the script waits between calls.
    A 2-year backfill (~500 trading days) takes about 1.5-2 hours, once.

Setup
  1. Get a free API key at https://massive.com (sign up, then copy the key
     from the dashboard). No credit card is needed for the free plan.
  2. Python 3.9+ is required. No extra packages are needed.
     (On Windows, run `pip install tzdata` once so US/Eastern time works.)
  3. Give the script your key, either as an environment variable:
         Windows (PowerShell):  $env:MASSIVE_API_KEY="your_key"
         macOS/Linux:           export MASSIVE_API_KEY="your_key"
     or with --api-key your_key on the command line.

Usage
  python otc_daily.py                         # backfill 2 years / update to yesterday
  python otc_daily.py --start 2026-01-01      # from a specific date
  python otc_daily.py --export-csv otc.csv    # also export everything to CSV
  python otc_daily.py --all-stocks            # keep listed stocks too, not only OTC

Notes
  - The free plan is end-of-day: a trading day becomes available after
    midnight US Eastern time (07:00 Israel time). Run it any time after that.
  - Prices are split-adjusted as of the day each row is downloaded. If a stock
    later does a split/reverse split, older rows you already stored are not
    re-adjusted. Use --refetch with a --start date to re-download a range,
    or --unadjusted to store raw prices instead.
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone

API_BASE = "https://api.massive.com"

# Optional hook called before every API request: throttle(sleep, log).
# The GUI sets it to a shared scheduler so all its requests stay under the
# plan's calls-per-minute limit. The command-line script leaves it unset.
throttle = None
GROUPED_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{day}"
FREE_PLAN_HISTORY_DAYS = 2 * 365 - 3   # stay just inside the 2-year window


# ----------------------------------------------------------------- helpers ---

def today_eastern() -> date:
    """Current date in US Eastern time (the market's calendar)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        # Fallback if timezone data is missing: approximate with UTC-5.
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:          # Mon-Fri; market holidays just return 0 rows
            yield d
        d += timedelta(days=1)


class FatalError(Exception):
    pass


def _get_json(url: str, what: str, max_retries: int = 6, sleep=time.sleep, log=print):
    """GET a Massive URL. Returns ("ok", payload) or ("not_authorized", body).
    Retries on rate limits, server errors and network errors."""
    for attempt in range(1, max_retries + 1):
        if throttle is not None:
            throttle(sleep, log)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "otc-daily-script/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return "ok", json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 401:
                raise FatalError("API key rejected (401). Check your Massive API key.\n" + body)
            if e.code == 403:
                # Outside what the plan allows (too old, or not yet published).
                return "not_authorized", body
            if e.code == 429:
                wait = 65
                log(f"    rate limit hit, waiting {wait}s (attempt {attempt}/{max_retries})")
                sleep(wait)
                continue
            if 500 <= e.code < 600:
                wait = 30 * attempt
                log(f"    server error {e.code}, retrying in {wait}s")
                sleep(wait)
                continue
            raise FatalError(f"HTTP {e.code} for {what}: {body}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait = 30 * attempt
            log(f"    network error ({e}), retrying in {wait}s")
            sleep(wait)
    raise FatalError(f"Giving up on {what} after {max_retries} attempts.")


def fetch_grouped_day(day: date, api_key: str, adjusted: bool, include_otc: bool,
                      max_retries: int = 6, sleep=time.sleep, log=print):
    """One trading day for the whole market. Returns (status, results).
    status is 'ok' or 'not_authorized'. `sleep` and `log` can be replaced
    (the GUI uses an interruptible sleep)."""
    params = urllib.parse.urlencode({
        "adjusted": "true" if adjusted else "false",
        "include_otc": "true" if include_otc else "false",
        "apiKey": api_key,
    })
    url = API_BASE + GROUPED_PATH.format(day=day.isoformat()) + "?" + params
    status, payload = _get_json(url, str(day), max_retries, sleep, log)
    if status != "ok":
        return status, payload
    return "ok", payload.get("results") or []


def fetch_ticker_history(ticker: str, start: date, end: date, api_key: str,
                         adjusted: bool = True, sleep=time.sleep, log=print):
    """Daily bars for ONE ticker over a date range, in a single call.
    Returns a list of dicts with a 'date' key (YYYY-MM-DD) added."""
    path = "/v2/aggs/ticker/{}/range/1/day/{}/{}".format(
        urllib.parse.quote(ticker, safe=""), start.isoformat(), end.isoformat())
    params = urllib.parse.urlencode({
        "adjusted": "true" if adjusted else "false",
        "sort": "asc", "limit": 50000, "apiKey": api_key,
    })
    status, payload = _get_json(API_BASE + path + "?" + params, ticker, sleep=sleep, log=log)
    if status != "ok":
        raise FatalError(f"Massive refused the history request for {ticker} "
                         f"(outside your plan's date range?): {payload}")
    bars = payload.get("results") or []
    for b in bars:
        # t = start of the day's bar in ms since epoch. Massive uses midnight or
        # 4pm US Eastern, which both fall on the same calendar date in UTC.
        b["date"] = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).date().isoformat()
    return bars


def fetch_otc_ticker_list(api_key: str, delay: float, sleep=time.sleep, log=print):
    """All active OTC tickers with company name and security type, from Massive's
    reference endpoint (1,000 per page). Returns a list of dicts."""
    url = API_BASE + "/v3/reference/tickers?" + urllib.parse.urlencode({
        "market": "otc", "active": "true", "limit": 1000, "apiKey": api_key})
    out, page = [], 0
    while url:
        if page:
            sleep(delay)
        page += 1
        status, payload = _get_json(url, f"ticker list page {page}", sleep=sleep, log=log)
        if status != "ok":
            raise FatalError(f"Massive refused the ticker list request: {payload}")
        results = payload.get("results") or []
        out.extend(results)
        log(f"Ticker names: page {page}, {len(out):,} tickers so far")
        nxt = payload.get("next_url")
        url = (nxt + ("&" if "?" in nxt else "?") + "apiKey=" + urllib.parse.quote(api_key)) if nxt else None
    return out


# ---------------------------------------------------------------- database ---

def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")   # lets the GUI read while a download writes
    con.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            ticker  TEXT    NOT NULL,
            date    TEXT    NOT NULL,      -- YYYY-MM-DD
            open    REAL,
            high    REAL,
            low     REAL,
            close   REAL,
            volume  REAL,                  -- shares traded that day
            vwap    REAL,
            trades  INTEGER,               -- number of trades
            otc     INTEGER NOT NULL,      -- 1 = OTC, 0 = exchange-listed
            PRIMARY KEY (ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_candles_date ON candles(date);
        CREATE TABLE IF NOT EXISTS fetched_days (
            date       TEXT PRIMARY KEY,
            rows       INTEGER NOT NULL,
            adjusted   INTEGER NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticker_details (
            ticker      TEXT PRIMARY KEY,
            name        TEXT,              -- company name (from Massive)
            type        TEXT,              -- security type, e.g. CS = common stock
            tier        TEXT,              -- OTC tier as imported, e.g. "Pink Limited"
            tier_group  TEXT,              -- normalized tier, see TIER_GROUPS
            names_updated TEXT,
            tier_updated  TEXT
        );
    """)
    return con


def store_day(con, day: date, results, otc_only: bool, adjusted: bool, log=print) -> int:
    day_str = day.isoformat()
    has_otc_flag = any("otc" in r for r in results)
    if otc_only and results and not has_otc_flag:
        log("    warning: response has no 'otc' field; keeping all tickers for this day")

    rows = []
    for r in results:
        is_otc = bool(r.get("otc", False))
        if otc_only and has_otc_flag and not is_otc:
            continue
        rows.append((
            r.get("T"), day_str, r.get("o"), r.get("h"), r.get("l"), r.get("c"),
            r.get("v"), r.get("vw"), r.get("n"), 1 if is_otc else 0,
        ))

    with con:
        con.execute("DELETE FROM candles WHERE date = ?", (day_str,))
        con.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute(
            "INSERT OR REPLACE INTO fetched_days VALUES (?,?,?,?)",
            (day_str, len(rows), 1 if adjusted else 0,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return len(rows)


def _only_rounded(stored, fresh) -> bool:
    """True if a fresh price is the stored one, or the stored one rounded to 4 decimals."""
    if stored == fresh:
        return True
    if stored is None or fresh is None:
        return False
    return abs(fresh - stored) <= 0.00005 + 1e-9 and abs(fresh - round(fresh, 4)) < 1e-9


def replace_ticker_history(con, ticker: str, bars, start: date, end: date) -> int:
    """Replace one ticker's rows in [start, end] with freshly downloaded bars, and return the
    number of rows it now has there.

    One ticker's history comes rounded to 4 decimals, which is coarse for sub-penny stocks, and
    can leave out a date the whole-market download has. So where a bar only differs by that
    rounding the finer stored prices stay. A date it leaves out also stays if the dates around
    it are unchanged: otherwise a split may have changed them, and the old row would be off."""
    stored = {r[0]: r[1:] for r in con.execute(
        "SELECT date, open, high, low, close, volume, vwap, trades FROM candles "
        "WHERE ticker = ? AND date BETWEEN ? AND ?", (ticker, start.isoformat(), end.isoformat()))}
    fresh = {b["date"]: b for b in bars}
    rows, unchanged = [], set()
    for day, b in fresh.items():
        new = (b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v"), b.get("vw"), b.get("n"))
        old = stored.get(day)
        if old and all(_only_rounded(x, y) for x, y in zip(old[:4], new[:4])):
            unchanged.add(day)
            # A different volume means the day was revised, so only the prices stay
            new = old if (old[4] or 0) == (new[4] or 0) else (*old[:4], *new[4:])
        rows.append((ticker, day, *new, 1))
    common = sorted(set(stored) & set(fresh))
    for day in set(stored) - set(fresh):
        i = bisect_left(common, day)
        around = common[max(i - 1, 0):i + 1]
        if around and unchanged.issuperset(around):
            rows.append((ticker, day, *stored[day], 1))
    with con:
        con.execute("DELETE FROM candles WHERE ticker = ? AND date BETWEEN ? AND ?",
                    (ticker, start.isoformat(), end.isoformat()))
        con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def store_ticker_names(con, tickers) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [(t.get("ticker"), t.get("name"), t.get("type"), now)
            for t in tickers if t.get("ticker")]
    with con:
        con.executemany("""
            INSERT INTO ticker_details (ticker, name, type, names_updated) VALUES (?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                name = excluded.name, type = excluded.type,
                names_updated = excluded.names_updated""", rows)
    return len(rows)


# ------------------------------------------------------------- OTC tiers ---

# Normalized tiers, best to worst. Used by the screener's tier filter.
TIER_GROUPS = ["OTCQX", "OTCQB", "Pink Current", "Pink Limited", "Pink No Info",
               "Pink (other)", "Expert / Grey", "Other", "Unknown"]


def tier_group(raw) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "Unknown"
    if "qx" in s:
        return "OTCQX"
    if "qb" in s or "venture" in s:
        return "OTCQB"
    if "expert" in s or "grey" in s or "gray" in s:
        return "Expert / Grey"
    if "pink" in s or s.startswith("pk"):
        if "limited" in s:
            return "Pink Limited"
        if "no info" in s or "no information" in s:
            return "Pink No Info"
        if "current" in s:
            return "Pink Current"
        return "Pink (other)"
    return "Other"


def _find_column(headers, candidates, contains=()):
    norm = {h: h.strip().lower().replace("_", " ") for h in headers}
    for c in candidates:
        for h, n in norm.items():
            if n == c:
                return h
    for c in contains:
        for h, n in norm.items():
            if c in n:
                return h
    return None


def import_tier_csv(con, path: str):
    """Import OTC tiers from a CSV, e.g. the download from OTC Markets' stock screener.
    Needs a symbol column and a tier/market column (detected by header name).
    Returns (imported_count, {group: count}, detected_columns)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(20000)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []
        sym_col = _find_column(headers, ["symbol", "ticker", "sym", "ticker symbol"], ["symbol"])
        tier_col = _find_column(headers, ["tier", "market", "market tier", "tier name",
                                          "otc tier", "venue"], ["tier", "market"])
        name_col = _find_column(headers, ["security name", "company name", "name",
                                          "company", "issuer name"], ["name"])
        if not sym_col or not tier_col:
            raise ValueError("Couldn't find a symbol column and a tier/market column.\n"
                             "Columns in this file: " + ", ".join(headers))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows, groups = [], {}
        for r in reader:
            sym = (r.get(sym_col) or "").strip().upper()
            if not sym:
                continue
            tier = (r.get(tier_col) or "").strip()
            grp = tier_group(tier)
            groups[grp] = groups.get(grp, 0) + 1
            name = (r.get(name_col) or "").strip() if name_col else None
            rows.append((sym, name or None, tier, grp, now))
    with con:
        con.executemany("""
            INSERT INTO ticker_details (ticker, name, tier, tier_group, tier_updated)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                tier = excluded.tier, tier_group = excluded.tier_group,
                tier_updated = excluded.tier_updated,
                name = COALESCE(ticker_details.name, excluded.name)""", rows)
    return len(rows), groups, {"symbol": sym_col, "tier": tier_col, "name": name_col}


def export_csv(con, path: str):
    cur = con.execute(
        "SELECT c.date, c.ticker, d.name, d.tier, c.open, c.high, c.low, c.close, "
        "c.volume, c.vwap, c.trades, c.otc "
        "FROM candles c LEFT JOIN ticker_details d ON d.ticker = c.ticker "
        "ORDER BY c.date, c.ticker")
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "name", "tier", "open", "high", "low", "close",
                    "volume", "vwap", "trades", "otc"])
        for row in cur:
            w.writerow(row)
            n += 1
    print(f"Exported {n:,} rows to {path}")


# -------------------------------------------------------------------- main ---

def main():
    p = argparse.ArgumentParser(description="Download daily OTC candles from Massive.")
    p.add_argument("--api-key", default=os.environ.get("MASSIVE_API_KEY"),
                   help="Massive API key (default: MASSIVE_API_KEY env variable)")
    p.add_argument("--start", type=date.fromisoformat,
                   help="first date YYYY-MM-DD (default: ~2 years ago, the free-plan limit)")
    p.add_argument("--end", type=date.fromisoformat,
                   help="last date YYYY-MM-DD (default: yesterday, US Eastern)")
    p.add_argument("--db", default="otc_daily.db", help="SQLite file (default: otc_daily.db)")
    p.add_argument("--all-stocks", action="store_true",
                   help="keep exchange-listed stocks too, not only OTC")
    p.add_argument("--unadjusted", action="store_true",
                   help="store raw prices (not split-adjusted)")
    p.add_argument("--delay", type=float, default=13.0,
                   help="seconds between calls (default 13 = under 5 calls/min)")
    p.add_argument("--refetch", action="store_true",
                   help="re-download days already in the database")
    p.add_argument("--export-csv", metavar="PATH",
                   help="after downloading, export the whole database to this CSV file")
    args = p.parse_args()

    if not args.api_key:
        sys.exit("No API key. Set MASSIVE_API_KEY or pass --api-key (see the top of this file).")

    today = today_eastern()
    end = args.end or (today - timedelta(days=1))
    start = args.start or (today - timedelta(days=FREE_PLAN_HISTORY_DAYS))
    if start > end:
        sys.exit(f"Start date {start} is after end date {end}.")

    adjusted = not args.unadjusted
    otc_only = not args.all_stocks
    con = open_db(args.db)
    done = {row[0] for row in con.execute("SELECT date FROM fetched_days")}

    # Newest first, so the most recent weeks are available within minutes.
    todo = [d for d in reversed(list(weekdays(start, end)))
            if args.refetch or d.isoformat() not in done]
    print(f"Range {start} -> {end}: {len(todo)} day(s) to fetch "
          f"({'OTC only' if otc_only else 'all stocks'}, "
          f"{'adjusted' if adjusted else 'unadjusted'}). DB: {args.db}")
    if todo:
        mins = len(todo) * args.delay / 60
        print(f"Estimated time: ~{mins:.0f} min. Safe to stop with Ctrl+C and resume later.")

    fetched = skipped = 0
    try:
        for i, day in enumerate(todo, 1):
            if i > 1:
                time.sleep(args.delay)
            status, results = fetch_grouped_day(day, args.api_key, adjusted, include_otc=True)
            if status == "not_authorized":
                print(f"[{i}/{len(todo)}] {day}: not available on your plan, skipped "
                      f"(outside the 2-year window, or not yet published)")
                skipped += 1
                continue
            n = store_day(con, day, results, otc_only, adjusted)
            fetched += 1
            note = "  (no trading - holiday?)" if not results else ""
            print(f"[{i}/{len(todo)}] {day}: {n:,} tickers saved{note}")
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command again to resume.")
    except FatalError as e:
        print(f"\nError: {e}")

    total_rows, total_days = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT date) FROM candles").fetchone()
    print(f"Done this run: {fetched} day(s) fetched, {skipped} skipped. "
          f"Database now has {total_rows:,} rows across {total_days} trading day(s).")

    if args.export_csv:
        export_csv(con, args.export_csv)
    con.close()


if __name__ == "__main__":
    main()
