"""
otc_fills.py - How much traded at the checklist's entry or lower, and at its exit or higher, in the last days: from
Schwab's 1-minute candles (any ticker, recorded or not), helped by our own record of the ticker (the server's
market_log, otc_recorder) where it has one.

Each regular session minute's volume, split around the price:
  - the whole minute at / past the price (its high at / under the entry, its low at / over the exit): all of it;
    none of it there: none;
  - a minute across the price (it traded at more than one price):
      - recorded (our record has trades in that minute: a trade = the day's volume up in a Level 1 update, at the
        last price): split as the recorded trades of that minute were (their share at / past the price);
      - not recorded: a quarter for each of its open, high, low and close at / past the price (each a trade).
        (Checked against the recorded trades of the favorites, 11/16-11/23: the closest of the rules tried; spreading
        a minute evenly over its range missed FOBIF's 544,739 at 0.0200 in a 0.0181-0.0200 minute.)
No Bid / Ask: the volume traded at the price, not only the trades that would have filled an order there.
"""

import threading
import time
from pathlib import Path

from otc_recorder import market_date, read_day, session

MARKET_LOG = Path(__file__).resolve().parent / "market_log"
EPS = 1e-9
CHECK = "✓"                     # a day with enough volume (Cascadia Mono has it, as wide as a digit)
ENOUGH_X = 2                    # a day had enough volume: this many times the shares
CANDLES_AGAIN_S = 60            # a ticker's 1-minute candles read from Schwab again after this long
_candles = {}                   # ticker -> (time read, Schwab's candles)
_candles_lock = threading.Lock()

EXPLAIN = ("From Schwab's 1-minute candles (any ticker), the regular session of the last days: a minute wholly at / "
           "past the price counts all its volume. A minute across the price (traded at more than one price): split "
           "as our record's trades of that minute were, when we recorded it; else a quarter for each of its open, "
           "high, low and close at / past the price. The volume traded there - not only the trades that would have "
           "filled your order (no Bid / Ask in a candle).")


def candles_of(ticker, settings, token_path):
    """Schwab's 1-minute candles of the ticker (otc_intraday.read_candles), kept CANDLES_AGAIN_S."""
    from otc_intraday import read_candles
    with _candles_lock:
        got = _candles.get(ticker)
    if got and time.time() - got[0] < CANDLES_AGAIN_S:
        return got[1]
    candles = read_candles(settings, token_path, ticker)
    with _candles_lock:
        _candles[ticker] = (time.time(), candles)
    return candles


def recorded_minutes(lines):
    """A day's record -> {minute's start (ms): [(price, shares)]}: its trades (the volume up in a Level 1 update)."""
    out, vol, last = {}, None, None
    for ln in lines:
        if ln.get("k") not in ("L1", "Q"):
            continue
        d = ln.get("d") or {}
        v = d.get("TOTAL_VOLUME")
        last = d.get("LAST_PRICE", last)
        if ln["k"] == "L1" and v is not None and vol is not None and v > vol and last:
            out.setdefault(ln["t"] // 60_000 * 60_000, []).append((last, v - vol))
        if v is not None:
            vol = v        # (the last one: a day's first can be the day before's total; a "Q": the count from there)
    return out


def _at(p, price, below):
    return p <= price + EPS if below else p >= price - EPS


def _part(candle, price, below, trades):
    """The share of a candle's volume at or below `price` (below) / at or above it: all or none when the whole minute
    was / wasn't; else its recorded trades' share, or a quarter for each of its open, high, low and close there."""
    low, high = candle["low"], candle["high"]
    if _at(high if below else low, price, below):
        return 1.0
    if not _at(low if below else high, price, below):
        return 0.0
    total = sum(s for _, s in trades or [])
    if total:
        return sum(s for p, s in trades if _at(p, price, below)) / total
    prices = [p for p in (candle.get(k) for k in ("open", "high", "low", "close")) if p is not None]
    return sum(1 for p in prices if _at(p, price, below)) / len(prices)


def candle_volumes(candles, entry, exit_, ticker=None, folder=MARKET_LOG):
    """The regular session's volume at or below the entry ("buy") and at or above the exit ("sell") (see the
    module's notes): {"days", "buy", "sell", "rows": [(day, buy, sell, minutes across a price, of them recorded)]
    oldest first}. `ticker`: its record used where there is one (None: candles only)."""
    per_day, times, records = {}, {}, {}
    for c in candles:
        ms, v = c.get("datetime"), float(c.get("volume") or 0)
        if ms is None or not v or c.get("low") is None or c.get("high") is None:
            continue
        day = market_date(ms)
        if day not in times:
            times[day] = session(day)
        if not times[day] or not times[day][0] <= ms < times[day][1]:
            continue
        row = per_day.setdefault(day, [0.0, 0.0, 0, 0])
        low, high = c["low"], c["high"]
        across = (low <= entry + EPS < high) or (low < exit_ - EPS <= high)
        trades = None
        if across:
            row[2] += 1
            if ticker and day not in records:
                try:
                    records[day] = recorded_minutes(read_day(folder, day.isoformat(), ticker))
                except Exception:
                    records[day] = {}
            trades = (records.get(day) or {}).get(ms // 60_000 * 60_000)
            if trades:
                row[3] += 1
        row[0] += v * _part(c, entry, True, trades)
        row[1] += v * _part(c, exit_, False, trades)
    rows = [(d.isoformat(), round(b), round(s), m, r) for d, (b, s, m, r) in sorted(per_day.items())]
    return {"days": len(rows), "buy": sum(r[1] for r in rows), "sell": sum(r[2] for r in rows), "rows": rows}


def candle_lines(vols, entry, exit_):
    """"BUY volume at price<=0.0131: 18,000 (7 days)", "SELL volume at price>=0.0160: 20,000 (7 days)"."""
    from otc_trade import fmt_price
    n = vols["days"]
    days = f" ({n} day{'s' if n != 1 else ''})"
    return [f"BUY volume at price<={fmt_price(entry)}: {vols['buy']:,}{days}",
            f"SELL volume at price>={fmt_price(exit_)}: {vols['sell']:,}{days}"]


def candle_day_lines(vols, entry, exit_):
    """A line per day, newest first: "11/23: BUY ≤ 0.0131 18,000 | SELL ≥ 0.0160 20,000 · 5 of 7 minutes across the
    prices from our record"."""
    from otc_trade import fmt_price
    out = []
    for d, b, s, across, rec in reversed(vols["rows"]):
        line = f"{int(d[5:7])}/{int(d[8:10])}: BUY ≤ {fmt_price(entry)} {b:,} | SELL ≥ {fmt_price(exit_)} {s:,}"
        if across:
            line += f" · {rec} of {across} minute{'s' if across != 1 else ''} across the prices from our record"
        out.append(line)
    return out


def candle_table_html(vols, entry, exit_, shares=0):
    """The hover: a table in a fixed-width font, newest day first - the day, the BUY (≤ entry) and SELL (≥ exit)
    volume - and a summary: the days with enough volume (ENOUGH_X times the shares) of them all."""
    import html
    from otc_trade import fmt_price
    head = ("Day", f"BUY ≤ {fmt_price(entry)}", f"SELL ≥ {fmt_price(exit_)}")
    days = list(reversed(vols["rows"]))
    need = ENOUGH_X * (shares or 0)
    ok = lambda v: bool(need) and v >= need
    rows = [(f"{int(r[0][5:7])}/{int(r[0][8:10])}", (ok(r[1]), f"{r[1]:,}"), (ok(r[2]), f"{r[2]:,}")) for r in days]
    summary = ("Summary", (False, f"{sum(1 for r in days if ok(r[1]))}/{len(days)}"),
               (False, f"{sum(1 for r in days if ok(r[2]))}/{len(days)}"))
    w0 = max(len(x[0]) for x in [head, summary] + rows)
    w = [max([len(head[i])] + [len(x[i][1]) + 2 for x in rows + [summary]]) for i in (1, 2)]
    cell = lambda c, i: (CHECK if c[0] else " ") + c[1].rjust(w[i] - 1)       # (the ✓ at the cell's left)
    line = lambda x: f"{x[0]:<{w0}} | {cell(x[1], 0)} | {cell(x[2], 1)}"
    rule = f"{'-' * w0}-|-{'-' * w[0]}-|-{'-' * w[1]}"
    top = f"{head[0]:<{w0}} | {head[1]:>{w[0]}} | {head[2]:>{w[1]}}"
    text = html.escape("\n".join([top, rule] + [line(x) for x in rows] + [rule, line(summary)]))
    text = text.replace(CHECK, f"<span style='color:#1db31d; font-weight:bold;'>{CHECK}</span>")
    return f"<pre style='font-family: \"Cascadia Mono\", Consolas, monospace;'>{text}</pre>"
