"""
otc_telegram_info.py - What a phone can ask the bot (otc_telegram.py), on the main server: the
favorites (/favs: a table, and a button per ticker), a ticker's details (its quote now, the volume
against its 30-day average, the top of Level II, the ATM trade, the alerts, the favorite's notes; with
Refresh), the open ATM trades (/trades) and the alerts (/alerts). Of the accounts the phone is linked to
only. Plain Python, no Qt.

Telegram can't color the text: the favorites' stars are colored emoji, the tables monospace (HTML <pre>).

Buttons' data (callback_data, up to 64 bytes): "i:f:<account>" the favorites, "i:t:<account>:<ticker>"
a ticker's details.
"""

import html
import sqlite3
from datetime import date, datetime
from pathlib import Path

import otc_alerts
from otc_trade import fmt_price, remaining

DB_PATH = Path(__file__).resolve().parent / "otc_daily.db"     # the daily candles (names, the volume's average)
# the favorites' groups (otc_daily_gui.FAV_GROUPS): their name and the emoji for the star's color
STARS = {1: ("Yellow", "🟡"), 2: ("Green", "🟢"), 3: ("Blue", "🔵"), 4: ("Cyan", "🩵"), 5: ("Magenta", "🟣"),
         6: ("Red", "🔴")}
# the ATM trades' steps (otc_checklist.STATES), with emoji for their colors
STEPS = {"buy_open": "🟥 Step 1/4: Wait for Buy", "bought": "🟨 Step 2/4: Sell the Shares",
         "sell_open": "🟦 Step 3/4: Wait for Sell", "sold": "🟩 Step 4/4: Done!",
         "canceled": "⬜ Transaction Canceled"}
REST_L1 = (("lastPrice", "LAST_PRICE"), ("bidPrice", "BID_PRICE"), ("askPrice", "ASK_PRICE"),
           ("bidSize", "BID_SIZE"), ("askSize", "ASK_SIZE"), ("closePrice", "CLOSE_PRICE"),
           ("netPercentChange", "NET_CHANGE_PERCENT"), ("totalVolume", "TOTAL_VOLUME"), ("openPrice", "OPEN_PRICE"),
           ("highPrice", "HIGH_PRICE"), ("lowPrice", "LOW_PRICE"))
BOOK_ROWS = 5                   # Level II rows shown a side
MAX_FAVS = 60                   # buttons in one message (Telegram allows 100)


def esc(text):
    return html.escape(str(text), quote=False)


def favorites(store, tail):
    """{ticker: favorite} of the account, in its groups' order."""
    pre = f"favorites/{tail}/"
    favs = {k[len(pre):]: it.get("data") for k, it in store.items.items() if k.startswith(pre)}
    favs = {t: f for t, f in favs.items() if isinstance(f, dict) and f.get("group") in STARS}
    return dict(sorted(favs.items(), key=lambda kv: (kv[1].get("group", 9), kv[0])))


def trades(store, tail, ticker=None):
    """[(key, trade)] of the account (of a ticker): the open ones first, then by time."""
    pre = f"atm/{tail}/"
    out = [(k, it.get("data")) for k, it in store.items.items() if k.startswith(pre)]
    out = [(k, e) for k, e in out if isinstance(e, dict) and (ticker is None or e.get("ticker") == ticker)]
    return sorted(out, key=lambda ke: (ke[1].get("state") in ("sold", "canceled"), str(ke[1].get("time") or ke[0])))


def alerts(store, tail, ticker=None):
    pre = f"{otc_alerts.PREFIX}{tail}/"
    out = [it.get("data") for k, it in store.items.items() if k.startswith(pre)]
    return sorted((a for a in out if isinstance(a, dict) and (ticker is None or a.get("ticker") == ticker)),
                  key=lambda a: (a.get("state") != "active", a.get("ticker", ""), str(a.get("created", ""))))


def daily(ticker, path=None, before=None):
    """{"name", "avg_volume" (the last VOLUME_DAYS days before today, or before `before` "YYYY-MM-DD"),
    "close" (the last day's)} from the database; {} without it."""
    try:
        con = sqlite3.connect(f"file:{path or DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        out = {}
        row = con.execute("SELECT name FROM ticker_details WHERE ticker = ?", (ticker,)).fetchone()
        if row and row[0]:
            out["name"] = row[0]
        rows = con.execute("SELECT volume, close, date FROM candles WHERE ticker = ? AND date < ? ORDER BY date DESC "
                           "LIMIT ?", (ticker, before or date.today().isoformat(), otc_alerts.VOLUME_DAYS)).fetchall()
        if rows:
            out["avg_volume"] = sum(r[0] or 0 for r in rows) / len(rows)
            out["close"], out["close_date"] = rows[0][1], rows[0][2]
        return out
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def change_pct(q, close=None):
    last = q.get("LAST_PRICE")
    if q.get("NET_CHANGE_PERCENT") is not None:
        return q["NET_CHANGE_PERCENT"]
    close = q.get("CLOSE_PRICE") or close
    return (last - close) / close * 100 if last and close else None


def pct_text(p):
    return "" if p is None else f"{p:+.1f}%"


def compact(n):
    n = n or 0
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return f"{n:.0f}"


def close_when(day):
    """'At the close of Tue 11-17' (day: 'YYYY-MM-DD')."""
    try:
        return f"At the close of {datetime.strptime(day, '%Y-%m-%d'):%a %m-%d}"
    except (TypeError, ValueError):
        return "At the last close"


def favs_text(tail, favs, quotes, avgs, when=""):
    """The favorites' table: star, ticker, last, change, volume (x its average)."""
    if not favs:
        return f"⭐ <b>Favorites, account ...{esc(tail)}</b>\nNone yet (the ☆ on a chart in OTC Assistant)."
    lines = []
    for t, f in favs.items():
        q = quotes.get(t) or {}
        last = q.get("LAST_PRICE")
        vol = q.get("TOTAL_VOLUME")
        avg = avgs.get(t)
        x = f" {vol / avg:.1f}x" if vol and avg else ""
        lines.append(f"{STARS[f['group']][1]} {t:<5} {fmt_price(last) if last else '-':>8} "
                     f"{pct_text(change_pct(q)):>7}{x}")
    return (f"⭐ <b>Favorites, account ...{esc(tail)}</b>" + (f"\n<i>{esc(when)}</i>" if when else "")
            + "\n<pre>" + esc("\n".join(lines)) + "</pre>\n"
            "Tap one for its details. (x: today's volume against its 30-day average)")


def favs_buttons(tail, favs):
    names = [(f"{STARS[f['group']][1]} {t}", f"i:t:{tail}:{t}") for t, f in list(favs.items())[:MAX_FAVS]]
    rows = [names[i:i + 3] for i in range(0, len(names), 3)]
    return rows + [[("🔄 Refresh", f"i:f:{tail}")]]


def book_table(bids, asks):
    """The top of Level II: bids | asks, BOOK_ROWS rows."""
    out = []
    for i in range(min(max(len(bids), len(asks)), BOOK_ROWS)):
        def cell(rows):
            if i >= len(rows):
                return " " * 17
            mm, price, size = rows[i]
            return f"{str(mm)[:4]:<4} {fmt_price(price):>6} {compact(size):>5}"
        out.append(f"{cell(bids)}|{cell(asks)}".rstrip())
    return out


def ticker_text(tail, ticker, fav, q, when, info, bids, asks, trade_list, alert_list):
    """A ticker's details, for the phone."""
    def field(name, value):
        return f"<b>{name}:</b> {value}"
    head = f"{STARS[fav['group']][1]} " if fav else ""
    lines = [f"{head}<b>{esc(ticker)}</b>"]
    if q:
        last = q.get("LAST_PRICE")
        lines.append(field("Last", f"{fmt_price(last) if last else '-'} {pct_text(change_pct(q, info.get('close')))}"))
        for k, name in (("BID", "Bid"), ("ASK", "Ask")):
            price = q.get(f"{k}_PRICE")
            if price:
                # Schwab's Level 1 size is 0 outside the session: then the book's shares at that price
                size = q.get(f"{k}_SIZE") or sum(r[2] or 0 for r in (bids if k == "BID" else asks)
                                                  if abs(r[1] - price) < 1e-9)
                lines.append(field(name, fmt_price(price) + (f" x {int(size):,}" if size else " x ?")))
        day = [field(n, fmt_price(q[k])) for k, n in (("OPEN_PRICE", "Open"), ("HIGH_PRICE", "High"),
                                                      ("LOW_PRICE", "Low")) if q.get(k)]
        if day:
            lines.append(" · ".join(day))
        vol, avg = q.get("TOTAL_VOLUME"), info.get("avg_volume")
        if vol is not None:
            lines.append(field("Volume", f"{int(vol):,}" + (f" ({vol / avg:.1f}x the {otc_alerts.VOLUME_DAYS}-day "
                                                            f"avg {compact(avg)})" if avg else "")))
    else:
        lines.append("No quote now (no Schwab login on the main server?)")
    if bids or asks:
        lines.append("<b>Level II:</b> (bids | asks)\n<pre>" + esc("\n".join(book_table(bids, asks))) + "</pre>")
    for _, e in trade_list[:2]:
        own = remaining(e) if e.get("state") not in ("buy_open", "canceled") else 0
        what = f"{e.get('shares', 0):,} @ {fmt_price(e.get('entry') or 0)}"
        if own:
            what = f"own {own:,} @ {fmt_price(e.get('entry') or 0)}"
        exit_ = e.get("sell_price") if e.get("state") == "sell_open" else e.get("exit-live") or e.get("exit")
        lines.append(field("ATM", f"{STEPS.get(e.get('state'), e.get('state'))}\n   {what}"
                           + (f", exit {fmt_price(exit_)}" if exit_ and e.get("state") not in ("sold", "canceled") else "")))
    for a in alert_list:
        lines.append(field("🔔 Alert", esc(otc_alerts.describe(a)) + (" (fired)" if a.get("state") == "triggered" else "")))
    if fav:
        own = [field(n, f"{fav[k]:,}" if isinstance(fav[k], int) else esc(fav[k]))
               for k, n in (("own", "Own"), ("want", "Want to buy")) if fav.get(k)]
        own += [field(n, fmt_price(fav[k])) for k, n in (("bid", "My bid"), ("ask", "My ask"))
                if isinstance(fav.get(k), (int, float)) and fav.get(k)]
        if own:
            lines.append(" · ".join(own))
        if fav.get("notes"):
            lines.append(field("📝 Notes", esc(fav["notes"])))
    if when:
        lines.append(f"<i>{esc(when)}</i>")
    return "\n".join(lines)


def ticker_buttons(tail, ticker):
    return [[("🔄 Refresh", f"i:t:{tail}:{ticker}"), ("⭐ Favorites", f"i:f:{tail}")]]


def trades_text(tail, items):
    if not items:
        return f"No open ATM trades in account ...{esc(tail)}."
    lines = [f"<b>ATM trades, account ...{esc(tail)}</b>"]
    for _, e in items:
        lines.append(f"<b>{esc(e.get('ticker', '?'))}</b> {STEPS.get(e.get('state'), e.get('state'))}\n"
                     f"   {e.get('shares', 0):,} @ {fmt_price(e.get('entry') or 0)}")
    return "\n".join(lines)


def alerts_text(tail, items):
    if not items:
        return f"No alerts in account ...{esc(tail)} (right-click a chart in OTC Assistant → Add price alert)."
    lines = [f"🔔 <b>Alerts, account ...{esc(tail)}</b>"]
    for a in items:
        fired = a.get("state") == "triggered"
        when = str(a.get("triggered_at", "")).replace("T", " ")[5:16] if fired else ""
        lines.append(f"{'⚪' if fired else '🟢'} <b>{esc(a.get('ticker', '?'))}</b> {esc(otc_alerts.describe(a))}"
                     + (f" (fired {when})" if fired else ""))
    return "\n".join(lines)
