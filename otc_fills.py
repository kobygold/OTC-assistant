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
  - the sell: once the buy has filled, an order at `exit` joins the Ask's queue the same way (the shares shown
    at exactly that price ahead of it); a trade on the Ask at or above the exit takes them, then the order's.
Each day on its own ("had the buy been placed that morning"). An estimate: shares ahead that left (canceled,
moved) are still counted ahead, merged trades are taken as one at the last price.
"""

from datetime import datetime, timezone

from otc_recorder import market_days_back, read_day, session
from pathlib import Path

MARKET_LOG = Path(__file__).resolve().parent / "market_log"
FILL_DAYS = 10          # the market days looked at (today's first), those recorded
EPS = 1e-9
LATE_START_MS = 30 * 60_000     # a day's record that started this long after the open: "from HH:MM" (a part only)


def _level(books, side, price):
    """The shares shown at exactly `price` on a side ("b" / "a") of the merged books (every venue)."""
    return sum(levels.get(round(price, 6), 0) for levels in (b.get(side, {}) for b in books.values()))


def replay_day(lines, times, entry, exit_, shares):
    """One market day: its record's lines (oldest first) and its session (open, close) in ms -> a dict, or None
    when nothing was recorded in the session. See the module's notes."""
    if not times:
        return None
    lines = [ln for ln in lines if times[0] <= ln.get("t", 0) < times[1]]
    if not lines:
        return None
    out = {"start": lines[0]["t"], "end": lines[-1]["t"], "open": times[0], "close": times[1], "trades": 0,
           "merged": 0, "volume": 0, "bid_vol": 0, "ask_vol": 0,                 # on the Bid <= entry, on the Ask >= exit
           "buy_ahead": None, "bought": 0, "buy_time": None,
           "sell_ahead": None, "sold": 0, "sell_time": None}
    bid = ask = last = vol = None
    books = {}
    for ln in lines:
        k, d, t = ln.get("k"), ln.get("d") or {}, ln["t"]
        if k == "L2":
            levels = {}
            for side in ("b", "a"):
                levels[side] = {}
                for price, sizes in d.get(side) or []:
                    levels[side][round(price, 6)] = levels[side].get(round(price, 6), 0) + sum(s for _, s in sizes)
            books[d.get("s", "N")] = levels
            if out["buy_ahead"] is None and not out["bought"]:
                out["buy_ahead"] = _level(books, "b", entry)        # (the order joins the queue: who's there)
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
            side = "ASK" if ask and last >= ask - EPS else "BID" if bid and last <= bid + EPS else "MID"
            if side == "BID" and last <= entry + EPS:
                out["bid_vol"] += size
                if out["bought"] < shares:
                    if out["buy_ahead"] is None:
                        out["buy_ahead"] = _level(books, "b", entry)
                    ahead = out.setdefault("_ahead_b", out["buy_ahead"])
                    used = min(ahead, size)
                    out["_ahead_b"] = ahead - used
                    out["bought"] = min(shares, out["bought"] + size - used)
                    if out["bought"] >= shares:
                        out["buy_time"] = t
                        out["sell_ahead"] = _level(books, "a", exit_)   # (the sell joins the Ask's queue)
            if side == "ASK" and last >= exit_ - EPS:
                out["ask_vol"] += size
                if out["buy_time"] is not None and t > out["buy_time"] and out["sold"] < shares:
                    ahead = out.setdefault("_ahead_a", out["sell_ahead"] or 0)
                    used = min(ahead, size)
                    out["_ahead_a"] = ahead - used
                    out["sold"] = min(shares, out["sold"] + size - used)
                    if out["sold"] >= shares:
                        out["sell_time"] = t
        if v is not None:
            vol = v        # (the last one: a day's first can be the day before's total; a "Q": the count from there)
        bid, ask = d.get("BID_PRICE", bid), d.get("ASK_PRICE", ask)
    out.pop("_ahead_b", None)
    out.pop("_ahead_a", None)
    if out["buy_ahead"] is None:
        out["buy_ahead"] = 0
    return out


def estimate(ticker, entry, exit_, shares, folder=MARKET_LOG, days=FILL_DAYS, millis=None):
    """The recorded days (today's first): [{"day": "YYYY-MM-DD", ...replay_day's}], and a summary
    {"days", "bought", "round_trips"}: the days recorded, those the buy would have filled on, and those the sell
    then filled too."""
    if not ticker or not entry or not exit_ or not shares or shares <= 0:
        return [], {"days": 0, "bought": 0, "round_trips": 0}
    rows = []
    for day in market_days_back(days, millis):
        r = replay_day(read_day(folder, day.isoformat(), ticker), session(day), entry, exit_, int(shares))
        if r is not None:
            rows.append(dict(r, day=day.isoformat()))
    return rows, {"days": len(rows), "bought": sum(1 for r in rows if r["buy_time"]),
                  "round_trips": sum(1 for r in rows if r["sell_time"])}


def et_time(millis):
    """"10:42" - a time of the market's day, US Eastern."""
    from otc_recorder import _et_offset_ms, market_date
    day = market_date(millis)
    return datetime.fromtimestamp((millis - _et_offset_ms(day)) / 1000, timezone.utc).strftime("%H:%M")


def summary_text(summary):
    """"Fill chances: the buy would have filled on 2 of 3 recorded days, the sell after it on 1"."""
    n = summary["days"]
    if not n:
        return "Fill chances: no recorded days of this ticker yet."
    return (f"Fill chances ({n} recorded day{'s' if n != 1 else ''}): the buy would have filled on {summary['bought']}"
            f", the sell after it on {summary['round_trips']}")


def day_lines(rows, entry, exit_, shares):
    """A line per recorded day: "11/23: BUY ✔ 10:42 · 41,308 sold ≤ 0.0131 (10,000 ahead) | SELL ✔ 14:05 · ..."."""
    from otc_trade import fmt_price
    out = []
    for r in rows:
        day = f"{int(r['day'][5:7])}/{int(r['day'][8:10])}"
        part = f" (from {et_time(r['start'])})" if r["start"] - r["open"] > LATE_START_MS else ""
        buy = (f"BUY ✔ {et_time(r['buy_time'])}" if r["buy_time"] else
               f"BUY ✘ {r['bought']:,} of {shares:,}") + (f" · {r['bid_vol']:,} sold ≤ {fmt_price(entry)}"
                                                         f" ({r['buy_ahead']:,} ahead)")
        if r["buy_time"]:
            sell = (f"SELL ✔ {et_time(r['sell_time'])}" if r["sell_time"] else f"SELL ✘ {r['sold']:,} of {shares:,}") + (
                f" · {r['ask_vol']:,} bought ≥ {fmt_price(exit_)} ({r['sell_ahead'] or 0:,} ahead)")
        else:
            sell = f"({r['ask_vol']:,} bought ≥ {fmt_price(exit_)} that day)"
        out.append(f"{day}{part}: {buy} | {sell}" + (f" · {r['merged']} of {r['trades']} trades merged"
                                                     if r["merged"] else ""))
    return out
