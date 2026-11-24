"""
otc_fills.py - Fill chances: would a buy at the checklist's entry, and then a sell at its exit, have filled on the
days we recorded the ticker (the server's market_log: Level 1 and Level II, otc_recorder)?

Each market day's regular session is replayed from its record:
  - a trade: the day's volume went up (a Level 1 update); its price is the last price, its size the volume added
    (several trades between two updates come as one: "merged"); on the Bid when at or below the Bid before it,
    on the Ask when at or above the Ask before it, else in between;
  - the buy: an order at `entry` joins the Bid's queue when the day's record starts; the shares already shown at
    exactly that price (the Level II, every venue) are ahead of it. A trade on the Bid at or below the entry
    takes the shares ahead first, then the order's (a sale lower than the entry went through every bid at the
    entry and above: the order's too);
  - the sell: on its own (a trade can be bought one day and sold another), an order at `exit` joins the Ask's
    queue when the day's record starts, the same way (the shares shown at exactly that price ahead of it); a
    trade on the Ask at or above the exit takes them, then the order's.
Each day on its own ("had the order been placed that morning"). An estimate: shares ahead that left (canceled,
moved) are still counted ahead, merged trades are taken as one at the last price.

Also from Schwab's 1-minute candles (any ticker, recorded or not: candle_volumes): the volume traded at or below the
entry and at or above the exit, the regular session of the last days. A candle tells no Bid / Ask and no prices of
its own trades: a minute whose whole range is at / past the price counts all its volume, one that crosses it a part -
the share of its price steps (0.0001 under $1) at / past the price.
"""

import threading
import time

from datetime import datetime, timezone

from otc_recorder import market_days_back, read_day, session
from pathlib import Path

MARKET_LOG = Path(__file__).resolve().parent / "market_log"
FILL_DAYS = 10          # the market days looked at (today's first), those recorded
EPS = 1e-9
LATE_START_MS = 30 * 60_000     # a day's record that started this long after the open: "from HH:MM" (a part only)
CANDLES_AGAIN_S = 60            # a ticker's 1-minute candles read from Schwab again after this long
_candles = {}                   # ticker -> (time read, Schwab's candles)
_candles_lock = threading.Lock()


def _level(books, side, price):
    """The shares shown at exactly `price` on a side ("b" / "a") of the merged books (every venue)."""
    return sum(levels.get(round(price, 6), 0) for levels in (b.get(side, {}) for b in books.values()))


def replay_day(lines, times, entry, exit_, shares):
    """One market day: its record's lines (oldest first) and its session (open, close) in ms -> a dict, or None
    when nothing was recorded in the session. See the module's notes. Each side twice: as if the order is shown
    (it becomes the best Bid / Ask when its price is better, and is taken first), and "_c" (cautious: not shown -
    a market maker may hold a customer's order without showing it, as FOBIF's sell at 0.0199 under the shown 0.0200:
    a trade reaches it only at exactly its price, or when its price wasn't better than the shown Bid / Ask then)."""
    if not times:
        return None
    lines = [ln for ln in lines if times[0] <= ln.get("t", 0) < times[1]]
    if not lines:
        return None
    out = {"start": lines[0]["t"], "end": lines[-1]["t"], "open": times[0], "close": times[1], "trades": 0,
           "merged": 0, "volume": 0, "buy_ahead": None, "sell_ahead": None}
    for c in ("", "_c"):
        out.update({"bid_vol" + c: 0, "ask_vol" + c: 0, "bought" + c: 0, "buy_time" + c: None,
                    "sold" + c: 0, "sell_time" + c: None})
    left = {}               # (side, mode) -> the shares still ahead of the order
    bid = ask = last = vol = None
    books = {}

    def take(side, mode, size, t):
        """A trade that reaches the order (side "b" buy / "a" sell): the shares ahead first, then the order's."""
        done, when = ("bought", "buy_time") if side == "b" else ("sold", "sell_time")
        if out[done + mode] >= shares:
            return
        ahead = left.setdefault((side, mode), out["buy_ahead" if side == "b" else "sell_ahead"] or 0)
        used = min(ahead, size)
        left[(side, mode)] = ahead - used
        out[done + mode] = min(shares, out[done + mode] + size - used)
        if out[done + mode] >= shares:
            out[when + mode] = t

    for ln in lines:
        k, d, t = ln.get("k"), ln.get("d") or {}, ln["t"]
        if k == "L2":
            levels = {}
            for side in ("b", "a"):
                levels[side] = {}
                for price, sizes in d.get(side) or []:
                    levels[side][round(price, 6)] = levels[side].get(round(price, 6), 0) + sum(s for _, s in sizes)
            books[d.get("s", "N")] = levels
            if out["buy_ahead"] is None:            # (the orders join the queues when the day starts: who's there)
                out["buy_ahead"] = _level(books, "b", entry)
            if out["sell_ahead"] is None:
                out["sell_ahead"] = _level(books, "a", exit_)
            continue
        if k not in ("L1", "Q"):
            continue
        v = d.get("TOTAL_VOLUME")
        last = d.get("LAST_PRICE", last)
        if k == "L1" and v is not None and vol is not None and v > vol and last:
            size = v - vol
            out["trades"] += 1
            out["volume"] += size
            if d.get("LAST_SIZE") and size > d["LAST_SIZE"]:
                out["merged"] += 1
            for key, price in (("buy_ahead", entry), ("sell_ahead", exit_)):
                if out[key] is None:
                    out[key] = _level(books, "b" if key == "buy_ahead" else "a", price)
            side = "ASK" if ask and last >= ask - EPS else "BID" if bid and last <= bid + EPS else "MID"
            at_entry, at_exit = abs(last - entry) < EPS, abs(last - exit_) < EPS
            if side == "BID" and last <= entry + EPS:
                out["bid_vol"] += size
                take("b", "", size, t)
            # (not shown: at exactly the entry, or the entry wasn't above the shown Bid - in its queue)
            if at_entry or (side == "BID" and last <= entry + EPS and bid is not None and entry <= bid + EPS):
                out["bid_vol_c"] += size
                take("b", "_c", size, t)
            if side == "ASK" and last >= exit_ - EPS:
                out["ask_vol"] += size
                take("a", "", size, t)
            # (not shown: at exactly the exit, or the exit wasn't under the shown Ask - in its queue)
            if at_exit or (side == "ASK" and last >= exit_ - EPS and ask is not None and exit_ >= ask - EPS):
                out["ask_vol_c"] += size
                take("a", "_c", size, t)
        if v is not None:
            vol = v        # (the last one: a day's first can be the day before's total; a "Q": the count from there)
        bid, ask = d.get("BID_PRICE", bid), d.get("ASK_PRICE", ask)
    for k in ("buy_ahead", "sell_ahead"):
        if out[k] is None:
            out[k] = 0
    return out


def estimate(ticker, entry, exit_, shares, folder=MARKET_LOG, days=FILL_DAYS, millis=None):
    """The recorded days (today's first): [{"day": "YYYY-MM-DD", ...replay_day's}], and a summary
    {"days", "bought", "sold", "bid_vol", "ask_vol"}: the days recorded, those the buy would have filled on, those
    the sell would have (each on its own), and the shares traded on the Bid at or below the entry / on the Ask at or
    above the exit, on them all."""
    if not ticker or not entry or not exit_ or not shares or shares <= 0:
        return [], {"days": 0, "bought": 0, "sold": 0, "bid_vol": 0, "ask_vol": 0,
                    "bought_c": 0, "sold_c": 0, "bid_vol_c": 0, "ask_vol_c": 0}
    rows = []
    for day in market_days_back(days, millis):
        r = replay_day(read_day(folder, day.isoformat(), ticker), session(day), entry, exit_, int(shares))
        if r is not None:
            rows.append(dict(r, day=day.isoformat()))
    out = {"days": len(rows)}
    for c in ("", "_c"):
        out.update({"bought" + c: sum(1 for r in rows if r["buy_time" + c]),
                    "sold" + c: sum(1 for r in rows if r["sell_time" + c]),
                    "bid_vol" + c: sum(r["bid_vol" + c] for r in rows),
                    "ask_vol" + c: sum(r["ask_vol" + c] for r in rows)})
    return rows, out


def et_time(millis):
    """"10:42" - a time of the market's day, US Eastern."""
    from otc_recorder import _et_offset_ms, market_date
    day = market_date(millis)
    return datetime.fromtimestamp((millis - _et_offset_ms(day)) / 1000, timezone.utc).strftime("%H:%M")


def summary_text(summary):
    """"Fill chances (5 recorded days) · buy: 2 days (1 if not shown) · sell: 2 (1) · Bid ≤ entry 150,142 (27,142) ·
    Ask ≥ exit 89,350 (5,000)" - in brackets: if the order isn't shown (cautious)."""
    n = summary["days"]
    if not n:
        return "Fill chances: no recorded days of this ticker yet."
    return (f"Fill chances ({n} recorded day{'s' if n != 1 else ''}) · buy: {summary['bought']} "
            f"({summary['bought_c']} if not shown) · sell: {summary['sold']} ({summary['sold_c']}) · Bid ≤ entry "
            f"{summary['bid_vol']:,} ({summary['bid_vol_c']:,}) · Ask ≥ exit {summary['ask_vol']:,} "
            f"({summary['ask_vol_c']:,})")


def day_lines(rows, entry, exit_, shares):
    """A line per recorded day: "11/23: BUY ✔ 12:34 (not shown ✘ 0) · 123,000 sold ≤ 0.0131 (63,000), 0 ahead |
    SELL ..." - in brackets: if the order isn't shown."""
    from otc_trade import fmt_price

    def mark(r, side, c=""):
        done, when = ("bought", "buy_time") if side == "b" else ("sold", "sell_time")
        return f"✔ {et_time(r[when + c])}" if r[when + c] else f"✘ {r[done + c]:,}"
    out = []
    for r in rows:
        day = f"{int(r['day'][5:7])}/{int(r['day'][8:10])}"
        part = f" (from {et_time(r['start'])})" if r["start"] - r["open"] > LATE_START_MS else ""
        buy = (f"BUY {mark(r, 'b')} (not shown {mark(r, 'b', '_c')}) · {r['bid_vol']:,} sold ≤ {fmt_price(entry)} "
               f"({r['bid_vol_c']:,}), {r['buy_ahead']:,} ahead")
        sell = (f"SELL {mark(r, 'a')} (not shown {mark(r, 'a', '_c')}) · {r['ask_vol']:,} bought ≥ "
                f"{fmt_price(exit_)} ({r['ask_vol_c']:,}), {r['sell_ahead']:,} ahead")
        out.append(f"{day}{part}: {buy} | {sell}" + (f" · {r['merged']} of {r['trades']} trades merged"
                                                     if r["merged"] else ""))
    return out


def _part(low, high, price, below):
    """The share of a candle's price steps (low..high) at or below `price` (below) / at or above it."""
    step = 0.0001 if high < 1 else 0.01
    steps = round((high - low) / step) + 1
    k = round(((price - low) if below else (high - price)) / step) + 1
    return max(0, min(steps, k)) / steps


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


def candle_volumes(candles, entry, exit_):
    """The regular session's volume at or below the entry ("buy") and at or above the exit ("sell") in 1-minute
    candles (see the module's notes): {"days", "buy", "sell", "rows": [(day, buy, sell)] oldest first}."""
    from otc_recorder import market_date, session
    per_day, times = {}, {}
    for c in candles:
        ms, v = c.get("datetime"), float(c.get("volume") or 0)
        low, high = c.get("low"), c.get("high")
        if ms is None or not v or low is None or high is None:
            continue
        day = market_date(ms)
        if day not in times:
            times[day] = session(day)
        if not times[day] or not times[day][0] <= ms < times[day][1]:
            continue
        row = per_day.setdefault(day, [0.0, 0.0])
        row[0] += v * _part(low, high, entry, True)
        row[1] += v * _part(low, high, exit_, False)
    rows = [(d.isoformat(), round(b), round(s)) for d, (b, s) in sorted(per_day.items())]
    return {"days": len(rows), "buy": sum(r[1] for r in rows), "sell": sum(r[2] for r in rows), "rows": rows}


def candle_lines(vols, entry, exit_):
    """"BUY volume at price<=0.0131: 18,000 (7 days)", "SELL volume at price>=0.0160: 20,000 (7 days)"."""
    from otc_trade import fmt_price
    n = vols["days"]
    days = f" ({n} day{'s' if n != 1 else ''})"
    return [f"BUY volume at price<={fmt_price(entry)}: {vols['buy']:,}{days}",
            f"SELL volume at price>={fmt_price(exit_)}: {vols['sell']:,}{days}"]


def candle_day_lines(vols, entry, exit_):
    """A line per day, newest first: "11/23: BUY ≤ 0.0131 18,000 | SELL ≥ 0.0160 20,000"."""
    from otc_trade import fmt_price
    return [f"{int(d[5:7])}/{int(d[8:10])}: BUY ≤ {fmt_price(entry)} {b:,} | SELL ≥ {fmt_price(exit_)} {s:,}"
            for d, b, s in reversed(vols["rows"])]
