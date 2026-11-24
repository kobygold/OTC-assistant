"""The trade checklist next to the chart: the items of checklist.txt in Hebrew, each with a green
check when its condition is met and a red X when it isn't. "auto" items are worked out from the
chart's candles and the live Level II, "manual" ones are clicked by hand (saved per ticker): empty,
then a check, then an X, then empty again, and
"info" ones show a value: the entry, exit and stop prices, the amount of stocks and so on.
The position size and the entry, exit and stop prices can be typed over: the position size is kept
for all tickers, the prices per ticker, and clearing a field brings back the automatic value."""

import html
import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QTimer, QRect, QRectF, QRegularExpression, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QPolygonF,
                         QRegularExpressionValidator)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QScrollArea, QSpinBox, QDoubleSpinBox, QToolButton, QToolTip, QVBoxLayout, QWidget)

from otc_orders import (ORDER_DONE, ORDERS_BACK_DAYS, limit_order, limit_text, market_day, order_info, position_info,
                        transaction_fills)
from otc_chart import fmt_price, fmt_volume
from otc_level2 import MM_RED, MM_YELLOW, reload_text
from otc_trade import (DONE, FEE_USD, HALF, STEP_NAMES, bid_fill, current_exit, estimate_fees, pl_lines, pl_summary,
                       sell_fee, status_word, trade_steps)

POSITION_USD = 150      # the position size, in $ (the default's default: the Settings tab sets it per account)


def default_position(settings):
    """This computer's default position $ (before it was kept per account, in the shared data), else
    POSITION_USD. The app's is ChecklistPanel.default_usd() (the account's)."""
    try:
        v = float(settings.get("position_default") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else POSITION_USD


def usd_plain(v):
    """$150 / $152.50: whole dollars without cents."""
    return f"${v:,.0f}" if abs(v - round(v)) < 1e-9 else f"${v:,.2f}"
FEE_HIGH = 7.0          # a fee above this is shown in the section titles' yellow
COVER_X = 2             # the stop: bids for at least this many times our amount of stocks
VOL_DAYS = 30           # market days in the volume checks
SPIKE_X = 2.0           # a day with this many times the average volume is unusual
MAX_DEAD = 10           # most days without trades in VOL_DAYS for a steady volume
DEAD_RUN = 4            # this many last market days all without trades: not steady
VOL_PART = 2.0          # the median volume must be at least this many times our amount of stocks
COPIED_MS = 1200        # how long the "Copied" popup stays, in ms
RANGE_DAYS = 30         # calendar days the bounce range must have lasted
NA = "N/A"              # a value that isn't known (yet)
EDITABLE = ("size", "entry", "exit", "stop")     # info values that can be typed over
# Info values with a mark too: a ✔ once they reach their target (profit in %, ratio as 1:X)
TARGETS = {"profit": 25.0, "ratio": 3.0}
MIN_SHARES, MIN_SHARES_PENNY = 10_000, 5_000     # the least amount of stocks (Pennies: an entry of 0.01 and up)


def min_shares_check(shares, entry):
    """(state, value, tip): the amount of stocks is at least MIN_SHARES (Pennies: MIN_SHARES_PENNY)."""
    if not shares or not entry:
        return UNKNOWN, NA, "No amount of stocks yet"
    need = MIN_SHARES_PENNY if entry >= 0.01 else MIN_SHARES
    why = "a Penny (0.01 and up)" if entry >= 0.01 else "under 0.01"
    return (OK if shares >= need else BAD, f"{shares:,}",
            f"At least {need:,} stocks (an entry of {fmt_price(entry)}: {why})"
            + ("" if shares >= need else f": {need - shares:,} short"))
# Once Buy ATM was pressed these show the plan sent, fixed, in a cell of their own. Entry and size
# show only that; the others keep a live cell beside it (exit and stop typed or automatic, as before
# the buy; the gains and losses worked out from them)
# the thin lines between the panel's groups of rows: above the ticker, and after these (the Mini App draws them too)
SPLIT_BEFORE = ("ticker",)
SPLIT_AFTER = ("ticker", "shares", "stop", "ratio", "fee", "loss_usd")
LOCKED = ("size", "entry", "exit", "stop", "profit", "loss", "profit_gross", "loss_gross", "profit_usd", "loss_usd",
          "ratio")
LOCKED_W = 84             # px: the fixed cells' width
FILLS_AGAIN_S = 60        # the fill chances worked out again this often (today's record grows)
ORDER_EDITS = ("size", "entry", "shares")   # an open buy's values that can be changed (the order is replaced)
CHANGED_COLOR = "#f0a020"   # a changed value in the change-order dialog
LIVE_INDENT = 6           # px: a live value's text, where the text in the live price boxes starts
LOCKED_INDENT = 8         # px: the fixed cells' left padding, for the values beside them to line up
PRICE_STEP = Decimal("0.0001")      # the +/- buttons; a typed whole number means that many steps (12 -> 0.0012)
ORDER_COLOR = "#4fc3f7"     # the plan's values once a BUY order was sent: fixed, not the live ones
# What is read from Schwab, and when. While the stream's account activity is on, an order event
# (placed, changed, canceled, filled) reads the orders and positions at once, and so does each
# (re)connect of the stream; nothing else is read on a timer unless it is needed:
PL_ASK_S = 60               # a sold trade's Schwab transactions: asked again this often while not all are there
PL_TRIES = 30               # ...this many times at most (then the estimate stays)
FOLLOWUP_MAX_S = 180        # Update Buy / Sell Partial: their second order goes out only this soon after approved
HISTORY_MAX = 100           # a trade's history: its notes kept, with their time (the newest this many)
ORDER_CHECK_S = 15          # the timer's tick; the Monitor tab's quotes while it is open (not streamed tickers)
POSITIONS_TAB_S = 60        # the positions while the Monitor tab is open (Schwab's Day P/L isn't streamed)
SAFETY_CHECK_S = 300        # the orders, while an ATM trade has an order working (a fill's event missed)
NO_STREAM_CHECK_S = 60      # all of it, while the account activity stream is off (Level II off, reconnecting)
ORDER_EVENT_DELAY_MS = 700  # an order event's check waits this long: the events come a few together
# An ATM trade's states: the name shown, its colour and its text colour (the table's price colours)
STATES = {"buy_open": ("Step 1/4: Wait for Buy", "#e0404f", "#ffffff"),
          "bought": ("Step 2/4: Sell the Shares", "#e3b23c", "#111111"),
          "sell_open": ("Step 3/4: Wait for Sell", "#4fc3f7", "#111111"),
          "sold": ("Step 4/4: Done!", "#1db31d", "#111111"),
          "canceled": ("Transaction Canceled", None, "#d8d8d8")}     # the buy canceled, nothing bought
CANCEL_COLOR = "#e3b23c"    # the Cancel Buy / Cancel Sell buttons
BUY_COLOR, SELL_COLOR = "#009300", "#cf0508"

OK, BAD, WARN, UNKNOWN, INFO, EMPTY = "ok", "bad", "warn", "unknown", "info", "empty"
MAYBE = "maybe"         # could be good or bad: a yellow ?
SOLVED = "solved"       # an X that a checked manual item solved: a green X, counted as passed
NOT_AVAILABLE = "na"    # nothing to check (no Level II yet, nobody on the Ask): a gray N/A
MARKS = {OK: ("✔", "#1db31d"), BAD: ("✘", "#e0404f"), WARN: ("!", "#f0a020"), UNKNOWN: ("?", "#6c6c6c"),
         EMPTY: ("☐", "#8a8a8a"), SOLVED: ("✘", "#1db31d"), MAYBE: ("?", "#f3c400"),
         NOT_AVAILABLE: (NA, "#6c6c6c")}


def set_mark(mark, state):
    """A checklist mark: its symbol in its color (N/A smaller, to fit the marks' column, and lower, in
    line with the item's text)."""
    symbol, color = MARKS[state]
    small = len(symbol) > 1
    mark.setContentsMargins(0, 4 if small else 0, 0, 0)
    mark.setText(f"<span style='color:{color}; font-size:7pt'>{symbol}</span>" if small else
                 f"<b style='color:{color}'>{symbol}</b>")
NEXT_MARK = {EMPTY: OK, OK: BAD, BAD: EMPTY}     # a click on a manual item

PANEL_STYLE = """
#checklist, #checklist QScrollArea, #checklist #cl_body { background: #101214; }
#checklist QLabel { color: #d8d8d8; }
#checklist QLabel#cl_title { font-size: 12pt; font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_section { font-weight: bold; color: #e3b23c; padding-top: 3px; }
#checklist QLabel#cl_value { font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_summary { color: #a8a8a8; }
#checklist QLabel#cl_fills { color: #c9a0ff; }
#checklist QToolButton { color: #c8c8c8; border: none; }
#checklist QToolButton#cl_copy { border-radius: 3px; padding: 1px; }
#checklist QToolButton#cl_copy:hover { background: #2c3136; }
#checklist QToolButton#cl_new_plan { background: transparent; border: none; border-radius: 3px;
                                      padding: 1px 2px; font-size: 11px; color: #e0e0e0; }
#checklist QToolButton#cl_new_plan:hover { background: #2c3136; }
#checklist QToolButton#cl_new_plan:pressed { background: #3a4046; }
#checklist QToolButton#cl_step { background: #1d2125; border: 1px solid #3a4046; border-radius: 2px;
                                  padding: 0px; font-size: 11px; font-weight: bold; }
#checklist QToolButton#cl_step:hover { background: #2c3136; }
#checklist QToolButton#cl_step:pressed { background: #3a4046; }
#checklist QToolButton#cl_reset, #checklist QToolButton#cl_pin { border-radius: 3px; padding: 0px; font-size: 15px; }
#checklist QToolButton#cl_reset:hover, #checklist QToolButton#cl_pin:hover { background: #2c3136; }
#checklist QToolButton#cl_reset:pressed, #checklist QToolButton#cl_pin:pressed { background: #3a4046; }
#checklist QLineEdit { background: #1b1e21; border: 1px solid #2c3136; border-radius: 3px;
                       padding: 0px 4px; font-weight: bold; }
#checklist QLineEdit:focus { border-color: #5a6670; }
#checklist QLineEdit:disabled { background: #101214; border-color: #1b1e21; }
#checklist QLineEdit[readOnly="true"] { background: #101214; }
#checklist QPushButton#cl_buy, #checklist QPushButton#cl_sell, #checklist QPushButton#cl_update {
    color: #ffffff; font-weight: bold; border-radius: 4px; padding: 3px 14px; border: 1px solid transparent; }
#checklist QPushButton#cl_buy, #checklist QPushButton#cl_update { background: #009300; }
#checklist QPushButton#cl_buy:hover, #checklist QPushButton#cl_update:hover { background: #00b300; }
#checklist QPushButton#cl_sell { background: #cf0508; }
#checklist QPushButton#cl_sell:hover { background: #e8262a; }
#checklist QPushButton#cl_bid { background: #e3b23c; color: #111111; font-weight: bold; border-radius: 4px;
                               padding: 3px 14px; border: 1px solid transparent; }
#checklist QPushButton#cl_bid:hover { background: #f0c75a; }
#checklist QPushButton#cl_cancel, #checklist QPushButton#cl_done {
    font-weight: bold; border-radius: 4px; padding: 3px 14px; border: 1px solid transparent; }
#checklist QPushButton#cl_cancel { background: #e3b23c; color: #111111; }
#checklist QPushButton#cl_cancel:hover { background: #f0c75a; }
#checklist QPushButton#cl_done { background: #6a4fb0; color: #ffffff; padding: 2px 10px; }
#checklist QPushButton#cl_done:hover { background: #7f62c8; }
#checklist QPushButton#cl_buy:disabled, #checklist QPushButton#cl_sell:disabled,
#checklist QPushButton#cl_cancel:disabled, #checklist QPushButton#cl_update:disabled {
    background: #1d2125; color: #5c6268; border-color: #2c3136; }
#checklist QLabel#cl_order { color: #4fc3f7; }
#checklist QLabel#cl_locked { color: #4fc3f7; font-weight: bold; background: transparent;
                              border: none; padding: 1px 0px 1px 5px; }
#checklist QLineEdit#cl_order_edit { background: transparent; border: none; padding: 1px 0px 1px 3px; }
"""


@dataclass
class Item:
    text: str
    kind: str       # "auto", "info" or "manual"
    key: str        # the rule's key; for a manual item, its own key or else its text
    solved_by: str = ""     # a manual item's key: when it's checked, this item's X turns green


def parse(path: Path):
    """(title, [(section title, [Item])]) from checklist.txt. The lines before the first section
    are the title (its first line) and the help text."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    title = lines[0].strip() if lines else ""
    sections, item = [], None
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
            item = None
        elif line.startswith("- ") and sections:
            item = Item(line[2:].strip(), "manual", line[2:].strip())
            sections[-1][1].append(item)
        elif item is not None and line.startswith("  ") and not line.startswith("   "):
            word = line.strip()
            for kind in ("auto", "info", "manual"):
                if word.startswith(kind + "(") and ")" in word:
                    item.kind, item.key = kind, word[len(kind) + 1:word.index(")")].strip()
            if "solved_by(" in word:
                rest = word[word.index("solved_by(") + len("solved_by("):]
                item.solved_by = rest[:rest.index(")")].strip() if ")" in rest else ""
            item = None     # only the first line under an item says its kind
    return title, sections


# ---------------------------------------------------------------- the numbers ---
def tick(price: float) -> float:
    return 0.0001 if price < 1 else 0.01


def is_trips(price: float) -> bool:
    return price < 0.001


def plan(bid, ask, bids, usd=POSITION_USD, entry=None, exit=None, stop=None, fee=FEE_USD, shares=None):
    """The trade from the quote and the Level II bids [(mm, price, size, level)], highest first:
    {entry, exit, shares, stop, profit, loss, ratio, profit_gross, loss_gross (in $, without the fees), profit_usd,
    loss_usd (with them), auto}; parts that can't be worked out are None.
    A given entry, exit, stop or amount of stocks (an order's) is used instead of the automatic one;
    "auto" holds the automatic {entry, exit, stop} (the stop for the amount of stocks actually used)."""
    out = dict.fromkeys(("entry", "exit", "shares", "stop", "profit", "loss", "ratio", "profit_gross", "loss_gross",
                         "profit_usd", "loss_usd"))
    auto = out["auto"] = dict.fromkeys(("entry", "exit", "stop"))
    if bid:
        auto["entry"] = round(bid if is_trips(bid) else bid + tick(bid), 6)
    if ask:
        auto["exit"] = round(ask if is_trips(ask) else ask - tick(ask), 6)
    out["entry"] = entry or auto["entry"]
    out["exit"] = exit or auto["exit"]
    if shares:
        out["shares"] = shares
    elif out["entry"] and usd:
        out["shares"] = round(usd / out["entry"])
    if out["shares"] and bids:
        # the bids under the entry only (the checklist's "bids below the entry": a new entry moves the stop,
        # and an open buy's own bid isn't counted); a trips entry joins the bid: the bids at it count too
        e = out["entry"]
        total = 0
        for _, price, size, _ in bids:
            if e and (price > e + 1e-12 if is_trips(e) else price >= e - 1e-12):
                continue
            total += size
            if total >= COVER_X * out["shares"]:
                auto["stop"] = price
                break
    out["stop"] = stop if stop is not None else auto["stop"]
    if out["entry"] and out["exit"]:
        out["profit"] = (out["exit"] - out["entry"]) / out["entry"] * 100
    if out["entry"] and out["stop"] is not None:
        out["loss"] = (out["entry"] - out["stop"]) / out["entry"] * 100
    # one buy's fee and one sell's (never more than the sale brings)
    if out["shares"] and out["entry"] and out["exit"]:
        out["profit_gross"] = out["shares"] * (out["exit"] - out["entry"])
        out["profit_usd"] = out["profit_gross"] - fee - sell_fee(fee, out["shares"] * out["exit"])
    if out["shares"] and out["entry"] and out["stop"] is not None:
        out["loss_gross"] = -out["shares"] * (out["entry"] - out["stop"])
        out["loss_usd"] = out["loss_gross"] - fee - sell_fee(fee, out["shares"] * out["stop"])
    if out["profit"] is not None and out["loss"]:
        out["ratio"] = out["profit"] / out["loss"]
    return out


def volume_stats(rows, market_days, period):
    """(median, [unusual days], days without trades, last days in a row without trades) from the
    candles (date, open, high, low, close, volume, ...). The median is the chart's MedianVolume: the
    last `period` days with trades. Unusual days and days without trades are counted over the given
    market days. None if there are no market days."""
    if not market_days or not rows:
        return None
    med = median([r[5] or 0.0 for r in rows[-period:]])
    by_day = {r[0]: r[5] or 0.0 for r in rows}
    vols = [by_day.get(d, 0.0) for d in market_days]
    spikes = [d for d, v in zip(market_days, vols) if med and v >= SPIKE_X * med]
    dead = sum(1 for v in vols if not v)
    run = 0
    for v in reversed(vols):
        if v:
            break
        run += 1
    return med, spikes, dead, run


def target_state(key, number):
    """(state, tip) of a number with a target (the potential profit %, the profit-to-loss ratio)."""
    need = TARGETS[key]
    state = UNKNOWN if number is None else OK if number >= need - 1e-9 else BAD
    return state, (f"At least {need:g}%" if key == "profit" else f"At least 1:{need:g}")


def range_check(rows, rng):
    """(state, value, tip) of the bounce range: it must have lasted RANGE_DAYS calendar days."""
    if not rows:
        return UNKNOWN, "", "No candles"
    if rng is None:
        return BAD, "", "No bounce range now (ATM tab's Min gap and Touch within)"
    age = (date.fromisoformat(rng.last_date) - date.fromisoformat(rng.since)).days
    return (OK if age >= RANGE_DAYS else BAD, "",
            f"{fmt_price(rng.low)} - {fmt_price(rng.high)} since {rng.since} ({age} days, {rng.legs} legs)")


def l2_checks(bids, asks, bid, ask, p, reloaders=None):
    """{no_diluter, buyers, cushion: (state, value, tip)} from a Level II book and the plan().
    reloaders: {yellow market maker: its reload events} (otc_recorder.find_reloads): diluters too."""
    r = {}
    reloaders = reloaders or {}
    # Who is first on the Ask: the market makers at the best (lowest) ask price
    best = min((price for _, price, *_ in asks), default=None)
    first = sorted({mm for mm, price, *_ in asks if price == best})
    red = [mm for mm in first if mm in MM_RED or mm in reloaders]
    yellow = [mm for mm in first if mm in MM_YELLOW and mm not in reloaders]
    at = f" first on the Ask ({fmt_price(best)})" if best is not None else ""
    if red:
        caught = [mm for mm in red if mm in reloaders]
        r["no_diluter"] = (BAD, "", "A diluter" + at + ": " + ", ".join(red) + "".join(
            f"\n{mm} refilled its offer while it was bought from:\n{reload_text(reloaders[mm])}" for mm in caught))
    elif yellow:
        r["no_diluter"] = (MAYBE, "", "A so-so market maker" + at + ": " + ", ".join(yellow)
                           + ". Could be good or bad")
    elif first:
        r["no_diluter"] = (OK, "", "First on the Ask: " + ", ".join(first))
    else:
        r["no_diluter"] = (NOT_AVAILABLE, "", "Nobody on the Ask")
    bid_total = sum(s for _, _, s, _ in bids)
    ask_total = sum(s for _, _, s, _ in asks)
    trips = is_trips(bid or ask or 1)
    good = bid_total >= ask_total if trips else ask_total <= 2 * bid_total
    r["buyers"] = (OK if good else BAD, "",
                   f"Bids {bid_total:,} · Asks {ask_total:,}"
                   + (" (Trips: Bids must be at least the Asks)" if trips else " (Asks up to 2x the Bids)"))
    if not p["shares"]:
        r["cushion"] = (UNKNOWN, "", "No entry price")
    elif p["auto"]["stop"] is None:
        r["cushion"] = (BAD, "", f"All the bids add up to {bid_total:,}, less than {COVER_X}x {p['shares']:,}")
    else:
        r["cushion"] = (OK, "", f"The bids reach {COVER_X}x at {fmt_price(p['auto']['stop'])}")
    return r


def volume_checks(rows, market_days, period, shares):
    """{avg_volume, no_spike, steady: (state, value, tip)} from the candles."""
    r = {}
    stats = volume_stats(rows, market_days, period)
    if stats is None:
        for key in ("avg_volume", "no_spike", "steady"):
            r[key] = (UNKNOWN, "", "No candles")
        r["avg_volume"] = (UNKNOWN, NA, "No candles")
        return r
    med, spikes, dead, run = stats
    about = f"MedianVolume ({period}), as on the chart (its Averaging days)"
    if not shares:
        r["avg_volume"] = (UNKNOWN, fmt_volume(med), about + ". No amount of stocks to compare it with yet")
    else:
        need = VOL_PART * shares
        r["avg_volume"] = (OK if med >= need else BAD, fmt_volume(med),
                           f"{about}: {med:,.0f}. It must be at least {VOL_PART:g} x our "
                           f"{shares:,} stocks = {need:,.0f}")
    r["no_spike"] = ((BAD, "", f"{SPIKE_X:g}x the median volume or more on " + ", ".join(spikes)
                      + ". Check it's not dilution.") if spikes else (OK, "", ""))
    r["steady"] = (OK if dead <= MAX_DEAD and run < DEAD_RUN else BAD, "",
                   f"{dead} of {len(market_days)} market days without trades (up to {MAX_DEAD} is fine)"
                   + (f" · the last {run} market days without trades (bad from {DEAD_RUN})" if run else ""))
    return r


# The checks the ATM scanner counts (its Checks column), in the checklist's order
SCAN_CHECKS = (("profit", "Potential profit"), ("ratio", "Profit-to-loss ratio"),
               ("range", "A steady price range for a month"), ("no_diluter", "No diluter on the Ask"),
               ("buyers", "More buyers than sellers"), ("cushion", "A safety net"),
               ("avg_volume", "Average volume"), ("steady", "Few dead days (no trades)"))


def scan_checks(rows, market_days, rng, period, q, bids, asks, has_l2, usd=POSITION_USD, reloaders=None):
    """The ATM scanner's checks of a ticker, with the automatic entry, exit and stop:
    {key: (state, tip)} for SCAN_CHECKS. q: its Level 1 (stream names), bids / asks: its Level II
    [(mm, price, size, level)], has_l2: a book came (else the Level II checks are unknown)."""
    bid = (q or {}).get("BID_PRICE") or (bids[0][1] if bids else None)
    ask = (q or {}).get("ASK_PRICE") or (asks[0][1] if asks else None)
    p = plan(bid, ask, bids, usd)
    out = {}
    for key in ("profit", "ratio"):
        state, need = target_state(key, p[key])
        number = p[key]
        shown = NA if number is None else (f"{number:+.1f}%" if key == "profit" else f"1:{number:.2f}")
        out[key] = (state, f"{shown} ({need})")
    out["range"] = range_check(rows, rng)[::2]
    if has_l2:
        out.update({k: v[::2] for k, v in l2_checks(bids, asks, bid, ask, p, reloaders).items()})
    else:
        for key in ("no_diluter", "buyers", "cushion"):
            out[key] = (NOT_AVAILABLE if key == "no_diluter" else UNKNOWN, "No Level II")
    vol = volume_checks(rows, market_days, period, p["shares"])
    out["avg_volume"] = vol["avg_volume"][::2]
    out["steady"] = vol["steady"][::2]
    return out


# ---------------------------------------------------------------- copying ---
def copy_value(text, widget, as_is=False):
    """Puts a shown value on the clipboard, as a plain number where it is one ($150 -> 150,
    3,055 -> 3055, +$32.23 -> +32.23), and says so in a tooltip. Nothing for an empty or unknown value.
    as_is: the text exactly as given (the trade plan)."""
    text = text.strip()
    if not text or text == NA:
        return
    if not as_is:
        text = text.replace("$", "").replace(",", "")
    QApplication.clipboard().setText(text)
    show_copied(f"Copied:\n{text}" if as_is else f"Copied: {text}")


_COPIED_POPUP = None


def show_copied(text, ms=COPIED_MS):
    """A small popup by the mouse for a moment (not a tooltip: those go at the click's release)."""
    global _COPIED_POPUP
    if _COPIED_POPUP is not None:
        try:
            _COPIED_POPUP.close()       # the one before, at once
        except RuntimeError:            # (already gone)
            pass
    pop = QLabel(text)
    pop.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
    pop.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    pop.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    pop.setStyleSheet("QLabel { background: #2a2f34; color: #ffffff; border: 1px solid #5a6670; "
                      "border-radius: 3px; padding: 3px 7px; font-weight: bold; }")
    pop.adjustSize()
    pop.move(QCursor.pos() + QPoint(12, 14))
    pop.show()
    _COPIED_POPUP = pop
    pop.destroyed.connect(lambda *_: globals().update(_COPIED_POPUP=None) if _COPIED_POPUP is pop else None)
    QTimer.singleShot(ms, pop.close)


def copy_icon():
    """Two overlapping pages, as copy icons usually are."""
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(QColor("#c8c8c8"), 1.2))
    p.drawRoundedRect(QRectF(4.5, 0.5, 8.5, 9.5), 1.5, 1.5)
    p.setBrush(QColor("#101214"))
    p.drawRoundedRect(QRectF(0.5, 3.5, 8.5, 9.5), 1.5, 1.5)
    p.end()
    return QIcon(pix)


def reset_icon(color="#c8c8c8"):
    """A smooth round arrow (clockwise, open at the top right), drawn at 4x and anti-aliased,
    so it stays sharp on any screen scaling."""
    scale, size = 4, 14
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(scale)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    ring = QRectF(2.5, 2.5, 9, 9)
    p.drawArc(ring, 60 * 16, 290 * 16)         # from 60° (top right) round to 350°
    # the arrowhead at the arc's start (60°), pointing along the clockwise turn
    cx, cy, r = 7, 7, 4.5
    a = math.radians(60)
    tip = QPointF(cx + r * math.cos(a), cy - r * math.sin(a))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    d = QPointF(math.sin(a), math.cos(a))          # clockwise tangent at the tip (screen y down)
    n = QPointF(math.cos(a), -math.sin(a))         # outward
    head = 4.2
    base = tip - d * head * 0.5
    p.drawPolygon(QPolygonF([tip + d * head * 0.6, base + n * head * 0.6, base - n * head * 0.6]))
    p.end()
    return QIcon(pix)


PIN_ON = "#f0a020"          # a pinned price's pin: the typed-over text's color
PIN_OFF = "#ffffff"         # not pinned: the automatic value


def pin_icon(pinned):
    """A push pin, drawn at 4x and anti-aliased: yellow and filled when pinned (as the pinned text), a white outline
    leaning over when not."""
    scale, size = 4, 14
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(scale)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    color = QColor(PIN_ON if pinned else PIN_OFF)
    p.translate(7, 7)
    if not pinned:
        p.rotate(35)
    pen = QPen(color, 1.2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(color if pinned else Qt.BrushStyle.NoBrush)
    # the head, the body narrowing down, the wide collar, then the needle
    p.drawRoundedRect(QRectF(-2.6, -6.2, 5.2, 1.8), 0.6, 0.6)
    p.drawPolygon(QPolygonF([QPointF(-1.6, -4.4), QPointF(1.6, -4.4), QPointF(2.2, 0.2), QPointF(-2.2, 0.2)]))
    p.drawLine(QPointF(-3.8, 0.6), QPointF(3.8, 0.6))
    p.drawLine(QPointF(0, 0.8), QPointF(0, 6.4))
    p.end()
    return QIcon(pix)


def price_text(text):
    """A typed price as the app shows prices (0.05 -> 0.0500); anything else as it is."""
    try:
        v = float(str(text).replace(",", "").strip())
    except ValueError:
        return text
    return fmt_price(v).replace(",", "") if v > 0 else text


def red_bang_icon(color="#d0101a"):
    """A bold red "!" for the Sell on Bid button, drawn at 4x and anti-aliased."""
    scale, size = 4, 12
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(scale)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 2.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(6, 1.8), QPointF(6, 7.2))
    p.drawPoint(QPointF(6, 10.2))
    p.end()
    return QIcon(pix)


def red_x_icon(color="#d0101a"):
    """A bold red X for the Cancel Buy / Cancel Sell buttons, drawn at 4x and anti-aliased."""
    scale, size = 4, 12
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(scale)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 2.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(2.5, 2.5), QPointF(9.5, 9.5))
    p.drawLine(QPointF(9.5, 2.5), QPointF(2.5, 9.5))
    p.end()
    return QIcon(pix)


class CopyBox(QWidget):
    """A value with a small copy button on its left (the line's left end), shown while the mouse is over them."""
    def __init__(self, value, shown_text, parent=None, copy_text=None):
        """shown_text: the value shown (the button shows only when there is one). copy_text: what
        to copy instead of it, exactly as given."""
        super().__init__(parent)
        self.shown_text = shown_text
        self.copy_text = copy_text
        self.copyable = True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.button = QToolButton()
        self.button.setObjectName("cl_copy")
        self.button.setIcon(copy_icon())
        self.button.setToolTip("Copy")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.copy)
        policy = self.button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)        # no jumping when it shows
        self.button.setSizePolicy(policy)
        self.button.hide()
        lay.addWidget(self.button)
        lay.addWidget(value)

    def copy(self):
        if self.copy_text:
            copy_value(self.copy_text(), self.button, as_is=True)
        else:
            copy_value(self.shown_text(), self.button)

    def set_copyable(self, on):
        """Without the copy button, and without its room."""
        if on == self.copyable:
            return
        self.copyable = on
        policy = self.button.sizePolicy()
        policy.setRetainSizeWhenHidden(on)
        self.button.setSizePolicy(policy)
        self.button.hide()

    def set_always(self, on):
        """The button shown all the time (not only under the mouse)."""
        self.always = on
        self.button.setVisible(on and self.copyable)

    def enterEvent(self, e):
        text = self.shown_text().strip()
        self.button.setVisible(self.copyable and bool(text) and text != NA)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.button.setVisible(getattr(self, "always", False) and self.copyable)
        super().leaveEvent(e)


class CopyLineEdit(QLineEdit):
    """A typed-over value. Double-click, Ctrl+C with nothing selected, or "Copy value" in the
    right-click menu copy the value shown: the one typed, or else the automatic one.
    Getting the focus selects it all, so typing replaces it; Enter applies it and leaves the box."""
    def shown(self):
        return self.text() or self.placeholderText()

    def mouseDoubleClickEvent(self, e):
        copy_value(self.shown(), self)
        super().mouseDoubleClickEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        if e.reason() != Qt.FocusReason.PopupFocusReason:
            QTimer.singleShot(0, self.selectAll)        # after the click has placed the cursor

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy) and not self.hasSelectedText():
            copy_value(self.shown(), self)
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()       # out of the box: that applies it (editingFinished), shown as it's kept
            return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        menu = self.createStandardContextMenu()
        first = menu.actions()[0] if menu.actions() else None
        action = menu.addAction("Copy value")
        if first:
            menu.insertAction(first, action)
            menu.insertSeparator(first)
        action.setEnabled(bool(self.shown()) and self.shown() != NA)
        action.triggered.connect(lambda: copy_value(self.shown(), self))
        menu.exec(e.globalPos())


# ---------------------------------------------------------------- the fee ---
class FeeReader(QObject):
    """Reads the fee of a buy from Schwab in a background thread: the order PREVIEW of a $150 GTC
    limit buy at the bid (nothing is placed), its commission plus fees. The sell is taken to cost
    the same. done(ticker, fee or None, details) arrives in the GUI thread."""
    done = pyqtSignal(str, object, str)
    accounts_read = pyqtSignal(object, str)     # [(last 3 digits, hash, type)], the error or ""

    def __init__(self, settings: dict, token_path: Path):
        super().__init__()
        self.settings, self.token_path = settings, token_path
        self._lock = threading.Lock()       # one Schwab call at a time
        self._client = None
        # [(last 3 digits, hash, type)] of the login's accounts, once logged in. The full numbers
        # are never kept or shown; settings["schwab_account"] holds the chosen one's last 3 digits.
        self.accounts = []

    def request(self, ticker):
        threading.Thread(target=self._read, args=(ticker,), name="fee", daemon=True).start()

    def _read(self, ticker):
        try:
            with self._lock:
                fee, details = self._preview(ticker)
            self.done.emit(ticker, fee, details)
        except Exception as e:      # never let the thread die silently
            self.done.emit(ticker, None, f"{type(e).__name__}: {e}")

    def connect(self, tail=None):
        """Logs in once (call it holding self._lock): the client and the hash of the account
        ending in `tail`, or else the chosen one (the first until one is chosen).
        Raises RuntimeError with the reason when it can't."""
        key = self.settings.get("schwab_app_key", "").strip()
        secret = self.settings.get("schwab_app_secret", "").strip()
        if not (key and secret and self.token_path.exists()):
            raise RuntimeError("No Schwab login (schwab_app_key, schwab_app_secret and the Level II login)")
        from otc_server_link import make_client
        if self._client is None:
            self._client = make_client(key, secret)     # on the server's token
        if not self.accounts:
            r = self._client.get_account_numbers()
            if r.status_code != 200 or not r.json():
                raise RuntimeError(f"Schwab accounts: HTTP {r.status_code}")
            types = {}
            try:        # Cash / Margin, for telling the accounts apart; fine without it
                a = self._client.get_accounts()
                if a.status_code == 200:
                    for acc in a.json():
                        sa = acc.get("securitiesAccount", {})
                        types[str(sa.get("accountNumber", ""))] = str(sa.get("type", "")).title()
            except Exception:
                pass
            self.accounts = [(str(x.get("accountNumber", ""))[-3:], x["hashValue"],
                              types.get(str(x.get("accountNumber", "")), "")) for x in r.json()]
        return self._client, self.account_hash(tail)

    def chosen_tail(self):
        """The last 3 digits of the account for the fee's preview: the one for orders, else the first."""
        tails = [tail for tail, _, _ in self.accounts]
        saved = self.settings.get("schwab_account")
        return saved if saved in tails else (tails[0] if tails else "")

    def order_tail(self):
        """The last 3 digits of the account orders go to, as chosen on the Monitor tab ("" if none)."""
        saved = self.settings.get("schwab_account", "")
        return saved if saved in [tail for tail, _, _ in self.accounts] else ""

    def kind(self, tail):
        """An account's name as shown: ...123 (Margin)."""
        kinds = {t: k for t, _, k in self.accounts}
        return f"...{tail}" + (f" ({kinds[tail]})" if kinds.get(tail) else "")

    def read_accounts(self, again=False):
        """Reads the login's accounts in a background thread (again: from Schwab, not the ones
        read before); accounts_read arrives in the GUI thread."""
        def run():
            try:
                with self._lock:
                    if again:
                        self.accounts = []
                    self.connect()
                self.accounts_read.emit(list(self.accounts), "")
            except Exception as e:
                self.accounts_read.emit([], str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")
        threading.Thread(target=run, name="accounts", daemon=True).start()

    def account_hash(self, tail=None):
        tail = tail or self.chosen_tail()
        for t, h, _ in self.accounts:
            if t == tail:
                return h
        raise RuntimeError(f"No Schwab account ending in {tail}")

    def load_accounts(self):
        """The accounts, logging in if needed (blocks: for the order dialogs). [] when it can't."""
        try:
            with self._lock:
                self.connect()
        except Exception:
            pass
        return self.accounts

    def _preview(self, ticker):
        try:
            self.connect()
        except RuntimeError as e:
            return None, str(e)
        from schwab.orders.common import Duration
        from schwab.orders.equities import equity_buy_limit
        r = self._client.get_quotes([ticker])
        quote = r.json().get(ticker, {}).get("quote", {}) if r.status_code == 200 else {}
        price = quote.get("bidPrice") or quote.get("lastPrice")
        if not price:
            return None, f"No bid for {ticker} to preview a buy"
        shares = max(1, round(POSITION_USD / price))
        limit = f"{price:.6f}".rstrip("0").rstrip(".")
        order = equity_buy_limit(ticker, shares, limit).set_duration(Duration.GOOD_TILL_CANCEL).build()
        r = self._client.preview_order(self.account_hash(), order)
        if r.status_code != 200:
            return None, f"Schwab order preview: HTTP {r.status_code} {r.text[:200]}"
        parts = r.json().get("commissionAndFee", {})
        commission = sum(v.get("value") or 0 for leg in parts.get("commission", {}).get("commissionLegs", [])
                         for v in leg.get("commissionValues", []))
        other = [(v.get("type"), v.get("value") or 0) for leg in parts.get("fee", {}).get("feeLegs", [])
                 for v in leg.get("feeValues", [])]
        if not parts:
            return None, "Schwab's order preview had no commission: " + json.dumps(r.json())[:200]
        fee = round(commission + sum(v for _, v in other), 2)
        extra = ", ".join(f"{name} ${v:.2f}" for name, v in other if v)
        return fee, (f"Schwab's order preview of a buy of {shares:,} {ticker} @ {limit}: commission "
                     f"${commission:.2f}" + (f", {extra}" if extra else ", no other fees")
                     + ". The sell is taken to cost the same.")


# ---------------------------------------------------------------- orders ---
class SchwabTrader(QObject):
    """Sends GTC limit orders to Schwab, cancels them and reads their status, in background
    threads, through the FeeReader's login (one client, one call at a time). Each request has a
    key (whose it is), given back with the answer in the GUI thread:
    placed(key, side, order id or None, message), checked(key, side, order or None, message),
    canceled(key, side, error or ""), quotes({ticker: Schwab quote}),
    orders(account tail, [order_info()] or None, error), replaced(key, side, new order id or None,
    price, error or ""). side is "BUY" or "SELL". order_set(ticker, side, price, account tail): a limit order
    Schwab took (made or changed), whoever sent it (the app's alert by it)."""
    placed = pyqtSignal(str, str, object, str)
    order_set = pyqtSignal(str, str, float, str)
    checked = pyqtSignal(str, str, object, str)
    canceled = pyqtSignal(str, str, str)
    quotes = pyqtSignal(object)
    orders = pyqtSignal(str, object, str)
    replaced = pyqtSignal(str, str, object, float, str)
    positions = pyqtSignal(str, object, str)    # account tail, [position_info()] or None, error
    sent = pyqtSignal(str, object, str)         # a sell from the stocks owned: key, order id or None, message
    transactions = pyqtSignal(str, object, str)     # key, [transaction_fills()] or None, error
    order_quote = pyqtSignal(str, str, object)      # account tail, order id, the market when it was sent

    def __init__(self, session: FeeReader):
        super().__init__()
        self.session = session
        # why orders can't be sent now ("" when they can): no main server (otc_computers); the
        # order actions then answer at once that they weren't sent
        self.blocked = lambda: ""
        self.link = None            # the server (use_server): it reads the orders and positions
        self.checks = {}            # (account, order id) -> [(key, side)]: checks waiting for the orders
        # the market now for a ticker (Bid / Ask and Level II), taken as each order goes out (AtmTrades sets it)
        self.snapshot_of = lambda ticker: None

    def _snap(self, ticker):
        try:
            return self.snapshot_of(ticker)
        except Exception:
            return None             # (never stops an order)

    def _keep_quote(self, account_tail, order_id, snap):
        """(worker thread) The order's market when it was sent, once Schwab gave its number."""
        if snap is not None and order_id:
            self.order_quote.emit(str(account_tail or self.session.chosen_tail() or ""), str(order_id), snap)

    def use_server(self, link):
        """From now on the orders and positions are read by the main server, once for all my
        computers, and come from it (to every app, asked or not); a check of one order is answered
        from its account's orders. Without a connection to the server, they are read here."""
        self.link = link
        link.message.connect(self._from_server)

    def _from_server(self, msg):
        kind = msg.get("type")
        if kind not in ("orders", "positions"):
            return
        account, data, error = msg.get("account", ""), msg.get("data"), msg.get("error", "")
        if kind == "positions":
            self.positions.emit(account, data, error)
            return
        self.orders.emit(account, data, error)
        if data is None:
            return                  # (the checks wait for the next read)
        by_id = {o["id"]: o for o in data}
        for key in [k for k in self.checks if k[0] == account]:
            for check_key, side in self.checks.pop(key):
                o = by_id.get(key[1])
                if o is not None:
                    self.checked.emit(check_key, side, {"orderId": o["id"], "status": o["status"],
                                                        "filledQuantity": o["filled"]}, "")
                else:               # not among them (older?): asked directly
                    self._start(self._check, check_key, side, key[1], account)

    def _refused(self, emit):
        """An order action not done (blocked): its answer, a moment later, as Schwab's would come."""
        why = self.blocked()
        if not why:
            return False
        text = f"Not sent: {why}"
        QTimer.singleShot(0, lambda: emit(text))
        return True

    def _taken(self, ticker, side, price, account_tail):
        """A limit order made or changed: order_set (to the GUI thread: a queued signal)."""
        try:
            if price:
                self.order_set.emit(ticker, side, float(price), str(account_tail or self.session.chosen_tail() or ""))
        except Exception:
            pass                    # (an alert's trigger: never the order's answer)

    def _start(self, target, *args):
        threading.Thread(target=target, args=args, name="order", daemon=True).start()

    def place(self, key, ticker, side, shares, price, account, aon=False):
        """account: the last 3 digits of the account to send it to. aon: All or none."""
        if self._refused(lambda why: self.placed.emit(key, side, None, why)):
            return
        self._start(self._place, key, ticker, side, shares, price, account, aon, self._snap(ticker))

    def check(self, key, side, order_id, account=None):
        if self.link is not None and self.link.is_connected:
            account = account or self.session.chosen_tail()
            self.checks.setdefault((account, str(order_id)), []).append((key, side))
            self.read_orders(account)
            return
        self._start(self._check, key, side, order_id, account)

    def cancel(self, key, side, order_id, account=None):
        if self._refused(lambda why: self.canceled.emit(key, side, why)):
            return
        self._start(self._cancel, key, side, order_id, account)

    def read_quotes(self, tickers):
        self._start(self._quotes, list(tickers))

    def read_orders(self, account):
        """The account's orders of the last ORDERS_BACK_DAYS days, as order_info()."""
        if self.link is not None and self.link.send({"type": "read", "what": "orders", "account": account}):
            return
        self._start(self._orders, account)

    def replace(self, key, ticker, side, order_id, shares, price, account, duration=None, session=None, aon=False):
        """Changes an open limit order's price, as thinkorswim does: Schwab replaces it with a new
        order (the old one becomes REPLACED). duration / session / aon: the old order's (GTC, NORMAL,
        not All or none when not given)."""
        if self._refused(lambda why: self.replaced.emit(key, side, None, price, why)):
            return
        self._start(self._replace, key, ticker, side, order_id, shares, price, account, duration, session, aon,
                    self._snap(ticker))

    def read_transactions(self, key, account, ticker, start, end):
        """The ticker's (None: every ticker's) TRADE transactions in the account between two datetimes (UTC):
        transactions(key, fills)."""
        self._start(self._transactions, key, account, ticker, start, end)

    def _transactions(self, key, account_tail, ticker, start, end):
        try:
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                r = client.get_transactions(account, start_date=start, end_date=end, symbol=ticker or None,
                                            transaction_types=[client.Transactions.TransactionType.TRADE])
            if r.status_code != 200:
                self.transactions.emit(key, None, f"HTTP {r.status_code}")
                return
            self.transactions.emit(key, transaction_fills(r.json() or [], ticker), "")
        except Exception as e:
            self.transactions.emit(key, None, f"{type(e).__name__}: {e}")

    def read_positions(self, account):
        """The stocks owned in the account, as position_info()."""
        if self.link is not None and self.link.send({"type": "read", "what": "positions", "account": account}):
            return
        self._start(self._positions, account)

    def send(self, key, ticker, side, shares, price, account, duration=None, order_type="LIMIT", aon=False):
        """An order that isn't an ATM trade (the stocks owned list, the chart's BUY / SELL): a limit
        order, or a market one (order_type "MARKET": price not used, always Day). The answer comes
        as sent()."""
        if self._refused(lambda why: self.sent.emit(key, None, why)):
            return
        self._start(self._send, key, ticker, side, shares, price, account, duration, order_type, aon,
                    self._snap(ticker))

    def _positions(self, account_tail):
        try:
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                r = client.get_account(account, fields=[client.Account.Fields.POSITIONS])
            if r.status_code != 200:
                self.positions.emit(account_tail, None, f"HTTP {r.status_code}")
                return
            raw = (r.json() or {}).get("securitiesAccount", {}).get("positions", []) or []
            self.positions.emit(account_tail, [p for p in map(position_info, raw) if p], "")
        except Exception as e:
            self.positions.emit(account_tail, None, f"{type(e).__name__}: {e}")

    def _send(self, key, ticker, side, shares, price, account_tail, duration, order_type="LIMIT", aon=False, snap=None):
        try:
            from schwab.utils import Utils
            order = (self._market_order(ticker, side, shares) if order_type == "MARKET"
                     else self._limit_order(ticker, side, shares, price, duration, aon=aon))
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                r = client.place_order(account, order)
                if r.status_code not in (200, 201):
                    self.sent.emit(key, None, f"Schwab refused the order: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    order_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    order_id = None
            if order_type != "MARKET":
                self._taken(ticker, side, price, account_tail)
            self._keep_quote(account_tail, order_id, snap)
            self.sent.emit(key, order_id, "")
        except Exception as e:
            self.sent.emit(key, None, f"{type(e).__name__}: {e}")

    @staticmethod
    def _market_order(ticker, side, shares):
        """A market order: Day only (Schwab takes no GTC market orders), the normal session."""
        from schwab.orders.common import Duration, Session
        from schwab.orders.equities import equity_buy_market, equity_sell_market
        make = equity_buy_market if side == "BUY" else equity_sell_market
        return make(ticker, int(shares)).set_duration(Duration.DAY).set_session(Session.NORMAL).build()

    @staticmethod
    def _limit_order(ticker, side, shares, price, duration=None, session=None, aon=False):
        return limit_order(ticker, side, shares, price, duration, session, aon)  # (otc_orders: the server's too)

    def _place(self, key, ticker, side, shares, price, account_tail, aon=False, snap=None):
        try:
            from schwab.utils import Utils
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                order = self._limit_order(ticker, side, shares, price, aon=aon)
                r = client.place_order(account, order)
                if r.status_code not in (200, 201):
                    self.placed.emit(key, side, None, f"Schwab refused the order: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    order_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    order_id = None
            self._taken(ticker, side, price, account_tail)
            self._keep_quote(account_tail, order_id, snap)
            self.placed.emit(key, side, order_id,
                             "" if order_id else "Schwab took the order but didn't say its number. "
                                                 "Check it in Schwab; the app can't follow it.")
        except Exception as e:      # never let the thread die silently
            self.placed.emit(key, side, None, f"{type(e).__name__}: {e}")

    def _check(self, key, side, order_id, account_tail):
        try:
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                r = client.get_order(order_id, account)
            if r.status_code != 200:
                self.checked.emit(key, side, None, f"HTTP {r.status_code}")
                return
            self.checked.emit(key, side, r.json(), "")
        except Exception as e:
            self.checked.emit(key, side, None, f"{type(e).__name__}: {e}")

    def _cancel(self, key, side, order_id, account_tail):
        try:
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                r = client.cancel_order(order_id, account)
            ok = r.status_code in (200, 201, 204)
            self.canceled.emit(key, side, "" if ok else f"HTTP {r.status_code} {r.text[:300]}")
        except Exception as e:
            self.canceled.emit(key, side, f"{type(e).__name__}: {e}")

    def _orders(self, account_tail):
        try:
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                # back 180 days, Schwab's longest Good-Till-Cancel (the library's default is 60)
                now = datetime.now()
                r = client.get_orders_for_account(account, from_entered_datetime=now - timedelta(days=ORDERS_BACK_DAYS),
                                                  to_entered_datetime=now)
                if r.status_code != 200:
                    r = client.get_orders_for_account(account)
            if r.status_code != 200:
                self.orders.emit(account_tail, None, f"HTTP {r.status_code}")
                return
            self.orders.emit(account_tail, [o for o in map(order_info, r.json() or []) if o], "")
        except Exception as e:
            self.orders.emit(account_tail, None, f"{type(e).__name__}: {e}")

    def _replace(self, key, ticker, side, order_id, shares, price, account_tail, duration, session, aon=False,
                 snap=None):
        try:
            from schwab.utils import Utils
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                order = self._limit_order(ticker, side, shares, price, duration, session, aon)
                r = client.replace_order(account, order_id, order)
                if r.status_code not in (200, 201):
                    self.replaced.emit(key, side, None, price,
                                       f"Schwab refused the change: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    new_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    new_id = None       # found among the account's orders at the next check
            self._taken(ticker, side, price, account_tail)
            self._keep_quote(account_tail, new_id, snap)
            self.replaced.emit(key, side, new_id, price, "")
        except Exception as e:
            self.replaced.emit(key, side, None, price, f"{type(e).__name__}: {e}")

    def _quotes(self, tickers):
        try:
            with self.session._lock:
                client, _ = self.session.connect()
                r = client.get_quotes(tickers)
            if r.status_code == 200:
                self.quotes.emit({t: v.get("quote", {}) for t, v in r.json().items() if isinstance(v, dict)})
        except Exception:
            pass        # the next check tries again


OPEN_GONE = {"CANCELED", "REPLACED"}
ORDER_QUOTES_DAYS = 180     # the orders' Bid / Ask then (and Level II then) are kept this long; their Actual$ too
ACTUALS_READ_S = 120        # the main server's app reads the fills' transactions at most this often an account...
ACTUALS_AGAIN_S = 900       # ...and again for the same orders not found yet (a fill shows a little later)     # an open order that may have been changed in thinkorswim


def tint(value) -> str:
    """The style attribute for a dialog value given as (text, colour); nothing for plain text."""
    return f" style='color:{value[1]}'" if isinstance(value, tuple) else ""


def usd_signed(v) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def fee_rows(fee, total, sell=False):
    """The total row of an order dialog, as the chart's BUY / SELL dialogs show it, in yellow:
    "$150.00 + $6.95 = $156.95" (a buy pays the commission on top), "$212.50 - $6.95 = $205.55" (a
    sell gets the total less it). fee None: not read from Schwab, the usual one is used (and said)."""
    usual = fee is None
    fee = FEE_USD if usual else fee
    if sell:
        fee = sell_fee(fee, total)      # (never more than the sale brings)
    net = total - fee if sell else total + fee
    text = (f"${total:,.2f} {'-' if sell else '+'} ${fee:.2f} = {'-' if net < 0 else ''}${abs(net):,.2f}"
            + (f" (the commission is more than ${FEE_HIGH:.2f}!)" if fee > FEE_HIGH else ""))
    label = ("$ less the commission" if sell else "$ with the commission") + (" (usual)" if usual else "")
    return [(label, (text, "#e3b23c"))]


PRICE_ROW = object()    # in confirm_price_order's rows: the row of the editable price


class PriceBox(QWidget):
    """An editable price, styled as the checklist's: the box, + above -, and a reset arrow back to
    `start`. The price shows as the box's white placeholder until changed (then orange, as a
    typed-over value). A typed whole number is a count of 0.0001 steps (12 -> 0.0012), as in the
    checklist. price_changed(float, 0 when not a price)."""
    price_changed = pyqtSignal(float)
    STYLE = PANEL_STYLE.replace("#checklist", "#price_box") + "#price_box { background: transparent; }"

    class Edit(CopyLineEdit):
        def focusInEvent(self, e):
            super().focusInEvent(e)
            if not self.text() and self.placeholderText():
                self.setText(self.placeholderText())    # the default, to type over or step from
            QTimer.singleShot(0, self.selectAll)        # the whole text selected (after a click's own)

        def keyPressEvent(self, e):
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.editingFinished.emit()     # takes the typed price; doesn't press a button
                e.accept()
                return
            super().keyPressEvent(e)

    def __init__(self, start, parent=None, reset_tip=None):
        super().__init__(parent)
        self.start = start
        self.setObjectName("price_box")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(self.STYLE)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.edit = self.Edit()
        self.edit.setFixedWidth(84)
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}(\.\d{0,6})?"), self.edit))
        pal = self.edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#f0a020"))
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#ffffff"))
        self.edit.setPalette(pal)
        self.edit.setPlaceholderText(fmt_price(start))
        self.edit.textChanged.connect(lambda _t: self.price_changed.emit(self.price()))
        self.edit.textChanged.connect(self._recolor)
        self.edit.editingFinished.connect(self._steps)
        lay.addWidget(self.edit)
        steps = QWidget()
        slay = QVBoxLayout(steps)
        slay.setContentsMargins(0, 0, 0, 0)
        slay.setSpacing(1)
        for sign, d in (("+", 1), ("−", -1)):
            b = QToolButton()
            b.setObjectName("cl_step")
            b.setText(sign)
            b.setFixedSize(18, 12)
            b.setToolTip(f"{'Up' if d > 0 else 'Down'} one step (0.0001 under $1, else 0.01)")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, s=d: self.step(s))
            slay.addWidget(b)
        lay.addWidget(steps)
        reset = QToolButton()
        reset.setObjectName("cl_reset")
        reset.setIcon(reset_icon())
        reset.setFixedSize(18, 25)
        reset.setToolTip(reset_tip or f"Back to {fmt_price(start)}, where it was dragged to")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.clicked.connect(self.edit.clear)
        lay.addWidget(reset)
        self.reset_btn = reset
        lay.addStretch()

    def price(self):
        try:
            v = float(self.edit.shown())
        except ValueError:
            return 0.0
        return v if v > 0 else 0.0

    def _recolor(self, *_):
        """Orange when typed over, white while it shows the default (as the placeholder)."""
        text = self.edit.text().replace(",", "")
        same = not text or text == self.edit.placeholderText().replace(",", "")
        pal = self.edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#ffffff" if same else "#f0a020"))
        self.edit.setPalette(pal)

    def set_price(self, value: Decimal):
        """Shown as everywhere in the app: 4 decimals under $1 (0.0300), 2 from $1."""
        self.edit.setText(fmt_price(float(value)).replace(",", ""))

    def step(self, direction):
        try:
            base = Decimal(self.edit.shown().strip())
        except InvalidOperation:
            return
        size = PRICE_STEP if base < 1 else Decimal("0.01")
        new = (base + direction * size).quantize(size)
        if new > 0:
            self.set_price(new)

    def _steps(self):
        try:
            typed = Decimal(self.edit.text().strip())
        except InvalidOperation:
            return
        if typed >= 1 and "." not in self.edit.text():
            typed *= PRICE_STEP
        if typed > 0:
            self.set_price(typed)           # 0.031 -> 0.0310


class AmountBox(PriceBox):
    """PriceBox's look for an amount: the $ ("usd", 2 decimals) or the shares (whole). The reset goes
    back to the default, default() (it may follow the price: refresh() after it changes), shown
    white as the placeholder until typed over. value_changed(float) as PriceBox's price_changed."""

    def __init__(self, kind, default, step, reset_tip, step_tip, parent=None):
        self.kind, self.default, self.step_of = kind, default, step
        super().__init__(0, parent, reset_tip=reset_tip)
        self.value_changed = self.price_changed
        self.edit.setFixedWidth(104)
        rx = r"[\d,]{0,12}(\.\d{0,2})?" if kind == "usd" else r"[\d,]{0,12}"
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(rx), self.edit))
        for b in self.findChildren(QToolButton):
            if b.objectName() == "cl_step":
                b.setToolTip(step_tip)
        self.refresh()

    def fmt(self, v):
        return f"{v:,.2f}" if self.kind == "usd" else f"{int(v):,}"

    def refresh(self):
        """The default shown again (when the price it follows changed)."""
        self.edit.setPlaceholderText(self.fmt(max(self.default() or 0, 0)))
        self._recolor()

    def price(self):
        try:
            v = float(self.edit.shown().replace(",", ""))
        except ValueError:
            return 0.0
        return v if v > 0 else 0.0
    value = price

    def set_value(self, v):
        self.edit.setText(self.fmt(max(v, 0)))

    def step(self, direction):
        size = self.step_of()
        v = (round(self.price() / size) + direction) * size
        self.set_value(max(v, 0))

    def _steps(self):
        if self.edit.text():
            self.set_value(self.price())        # 1500 -> 1,500.00 / 1,500


def confirm_price_order(parent, account_name, start, make, action, color=BUY_COLOR):
    """confirm_order with an editable price: make(price) -> (title, rows) for a price, rows with
    (label, PRICE_ROW) where the price box goes; the title and rows follow the price as it's
    changed. start: the price it opens with. The price chosen, or None when canceled."""
    box = PriceBox(start)
    title, rows = make(start)
    dlg, title_label, values, send = _order_dialog(parent, account_name, title, rows, action, color, box)

    def changed(price):
        ok = price > 0
        send.setEnabled(ok)
        if not ok:
            return
        t, new_rows = make(price)
        title_label.setText(f"<b>{t}</b>")
        dlg.setWindowTitle(t)
        for label, (_k, v) in zip(values, new_rows):
            if label is not None:
                label.setText(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>")
    box.price_changed.connect(changed)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    box._steps()            # a whole number typed and sent at once
    return box.price() or None


def sell_dialog(parent, account_name, ticker, owned, in_orders, start, fee, where, note=""):
    """Selling stocks owned (not an ATM trade): the amount (all those not in sell orders yet), the
    limit price (from the bid or ask: `where`), Day or GTC, and the $ with the commission, in yellow:
    "$101.00 - $6.95 = $94.05", and All or none (unchecked). (shares, price, duration, aon) or None when
    canceled."""
    free = max(int(owned - in_orders), 0)
    aon = aon_box()
    amount = QSpinBox()
    amount.setRange(1, max(free, 1))
    amount.setValue(max(free, 1))
    amount.setGroupSeparatorShown(True)
    amount.setMinimumWidth(120)
    amount.setToolTip(f"Up to {free:,}: the {owned:,} owned" + (f", less {in_orders:,} in sell orders" if in_orders else ""))
    all_btn = QPushButton("All")
    all_btn.setToolTip(f"All {free:,}")
    all_btn.clicked.connect(lambda: amount.setValue(free))
    amount_row = QWidget()
    lay = QHBoxLayout(amount_row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(amount)
    lay.addWidget(all_btn)
    lay.addStretch()
    duration = QComboBox()
    duration.addItem("GTC (good till canceled)", "GOOD_TILL_CANCEL")
    duration.addItem("Day", "DAY")
    box = PriceBox(start)
    usual = fee is None
    fee = FEE_USD if usual else fee

    def make(shares, price):
        total = shares * price
        taken = sell_fee(fee, total)        # (never more than the sale brings)
        net = total - taken
        dollars = f"${total:,.2f} - ${taken:.2f} = {'-' if net < 0 else ''}${abs(net):,.2f}"
        rows = [("Ticker", ticker), ("Order", "SELL · Limit · from the stocks owned"),
                ("Owned", f"{owned:,}" + (f" ({in_orders:,} in sell orders)" if in_orders else "")),
                ("Amount of stocks", amount_row), ("Limit price", PRICE_ROW), ("Duration", duration),
                ("Fill", aon), ("$ less the commission" + (" (usual)" if usual else ""), (dollars, "#e3b23c"))]
        if note:
            rows.append(("Note", (note, "#f0a020")))
        return f"Sell {shares:,} {ticker} at {fmt_price(price)} (on the {where})?", rows
    title, rows = make(amount.value(), start)
    dlg, title_label, values, send = _order_dialog(parent, account_name, title, rows, "Sell", SELL_COLOR, box)

    def changed(*_):
        price = box.price()
        send.setEnabled(price > 0 and free > 0)
        if price <= 0:
            return
        t, new_rows = make(amount.value(), price)
        title_label.setText(f"<b>{t}</b>")
        dlg.setWindowTitle(t)
        for label, (_k, v) in zip(values, new_rows):
            if label is not None:
                label.setText(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>")
    box.price_changed.connect(changed)
    amount.valueChanged.connect(changed)
    changed()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    box._steps()
    price = box.price()
    return (amount.value(), price, duration.currentData(), aon.isChecked()) if price and free > 0 else None


def partial_sell_dialog(parent, account_name, e, start, fee):
    """! Sell Partial: a partly filled ATM buy's stocks sold, the rest of the buy canceled first. The amount
    (all those bought so far, or fewer), the limit price (from `start`, the exit now; the reset: the plan's
    exit), +/- and the reset by each, white while they show the default and yellow once changed, All or none
    (unchecked). (shares, price, aon) or None when canceled."""
    filled, entry = int(e.get("filled") or 0), e["entry"]
    plan_exit = e.get("exit") or start
    box = PriceBox(plan_exit, reset_tip=f"Back to {fmt_price(plan_exit)}, the plan's exit")
    if start and abs(start - plan_exit) > 1e-9:
        box.set_price(Decimal(str(start)))      # changed since the plan (pinned on the checklist...): yellow
    amount = AmountBox("shares", lambda: filled, lambda: 1000 if box.price() < 1 else 10,
                       f"Back to all {filled:,} bought so far", "Up / down 1,000 shares under $1, else 10")
    amount.setToolTip(f"Up to {filled:,}: those bought so far")
    aon = aon_box()
    usual = fee is None
    fee = FEE_USD if usual else fee

    def make(shares, price):
        total = shares * price
        gain = shares * (price - entry) - fee - sell_fee(fee, total)
        rest = int(e.get("shares") or 0) - filled
        rows = [("Ticker", e["ticker"]), ("Order", "SELL · Limit · GTC (good till canceled)"),
                ("Bought so far", f"{filled:,} of {e.get('shares', 0):,} @ {fmt_price(entry)}"),
                ("The rest of the buy", (f"canceled first ({rest:,} not bought yet), then this sell goes out", "#f0a020")),
                ("Amount of stocks", amount), ("Limit price", PRICE_ROW)] + fee_rows(fee, total, sell=True)
        rows += [("Profit after both fees", usd_signed(gain)), ("Fill", aon)]
        return f"Sell {shares:,} {e['ticker']} at {fmt_price(price)} (partly bought)?", rows
    title, rows = make(filled, box.price())
    dlg, title_label, values, send = _order_dialog(parent, account_name, title, rows, "Cancel the rest + SELL",
                                                   SELL_COLOR, box)

    def changed(*_):
        price, shares = box.price(), int(amount.value())
        ok = price > 0 and 0 < shares <= filled
        send.setEnabled(ok)
        send.setToolTip("" if ok else f"The amount: 1 to {filled:,}")
        if not ok:
            return
        t, new_rows = make(shares, price)
        title_label.setText(f"<b>{t}</b>")
        dlg.setWindowTitle(t)
        for label, (_k, v) in zip(values, new_rows):
            if label is not None:
                label.setText(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>")
    box.price_changed.connect(changed)
    amount.value_changed.connect(changed)
    changed()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    box._steps()
    amount._steps()
    price, shares = box.price(), int(amount.value())
    return (shares, price, aon.isChecked()) if price > 0 and 0 < shares <= filled else None


def buy_more_dialog(parent, account_name, e, start, default_shares, fee):
    """Buy More: an ATM trade bought, more of it bought (the same trade). The amount (default_shares), the limit
    price (from `start`: the bid + 1 tick; the reset goes back to it), +/- and the reset by each, white while
    they show the default and yellow once changed, All or none (unchecked). (shares, price, aon) or None."""
    owned, entry = int(e.get("filled") or 0) - int(e.get("sold") or 0), e["entry"]
    box = PriceBox(start, reset_tip=f"Back to {fmt_price(start)}, the bid + 1 tick")
    amount = AmountBox("shares", lambda: default_shares, lambda: 1000 if box.price() < 1 else 10,
                       f"Back to {default_shares:,}", "Up / down 1,000 shares under $1, else 10")
    amount.setToolTip("The shares to buy now (added to the trade)")
    aon = aon_box()
    usual = fee is None
    fee = FEE_USD if usual else fee

    def make(shares, price):
        total = shares * price
        after = owned + shares
        avg = (owned * entry + total) / after if after else price
        exit_ = e.get("exit") or 0
        rows = [("Ticker", e["ticker"]), ("Order", "BUY · Limit · GTC · more for the ATM trade"),
                ("Own now", f"{owned:,} @ {fmt_price(entry)}"),
                ("Amount of stocks", amount), ("Limit price", PRICE_ROW)] + fee_rows(fee, total)
        rows.append(("Once bought", f"{after:,} @ {fmt_price(round(avg, 6))} (the average)"))
        if exit_:
            rows.append((f"Profit at the exit {fmt_price(exit_)}",
                         usd_signed(after * (exit_ - avg) - 3 * fee)))      # (this buy's fee, the first, the sell)
        rows.append(("Fill", aon))
        return f"Buy {shares:,} more {e['ticker']} at {fmt_price(price)}?", rows
    title, rows = make(default_shares, box.price())
    dlg, title_label, values, send = _order_dialog(parent, account_name, title, rows, "Send BUY order", BUY_COLOR, box)

    def changed(*_):
        price, shares = box.price(), int(amount.value())
        ok = price > 0 and shares > 0
        send.setEnabled(ok)
        if not ok:
            return
        t, new_rows = make(shares, price)
        title_label.setText(f"<b>{t}</b>")
        dlg.setWindowTitle(t)
        for label, (_k, v) in zip(values, new_rows):
            if label is not None:
                label.setText(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>")
    box.price_changed.connect(changed)
    amount.value_changed.connect(changed)
    changed()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    box._steps()
    amount._steps()
    price, shares = box.price(), int(amount.value())
    return (shares, price, aon.isChecked()) if price > 0 and shares > 0 else None


def order_dialog(parent, account_name, ticker, side, start, fee, owned=None, in_orders=0, note="", usd_start=0.0,
                 remember_usd=None, quotes=None):
    """The chart's BUY / SELL (not an ATM trade): Limit (the default) or Market, GTC (the default)
    or Day, and three boxes alike (+/-, reset, the whole text selected on entering): the price (from
    the chart's y where right-clicked), the $ and the shares, each of these two following the other.
    The $ with the commission in yellow: "$101.00 + $6.95 = $107.95" (a sell: less it).
    The defaults (the resets): a buy's $ is usd_start (the last one bought with) and its shares
    what it buys; a sell's shares are all those owned not in sell orders yet (owned less in_orders;
    owned None: none known) and its $ what they come to. remember_usd($): called with a buy's $
    when it is sent. quotes(): {"bid", "ask", "current"} now (None when not known), for the quick
    price buttons over the price (with "Clicked": start), read again every second.
    {"shares", "price" (None at Market), "duration", "type", "aon"} or None when canceled."""
    selling = side == "SELL"
    usual = fee is None
    fee = FEE_USD if usual else fee
    free = None if owned is None else max(int(owned - in_orders), 0)
    kind = QComboBox()
    kind.addItem("Limit", "LIMIT")
    kind.addItem("Market", "MARKET")
    kind.setToolTip("Market: at the market's price now, Day only (Schwab takes no GTC market orders)")
    duration = QComboBox()
    duration.addItem("GTC (good till canceled)", "GOOD_TILL_CANCEL")
    duration.addItem("Day", "DAY")
    box = PriceBox(start, reset_tip=f"Back to {fmt_price(start)}, the chart's price where it was right-clicked")

    def price():
        return box.price()

    def default_shares():
        if selling:
            return free or 0
        p = price()
        return int(usd_start / p + 1e-9) if p > 0 and usd_start > 0 else 0

    def default_usd():
        return (free or 0) * price() if selling else usd_start

    def shares_step():
        return 1000 if price() < 1 else 10
    usd = AmountBox("usd", default_usd, lambda: 10,
                    ("Back to all the shares' $ at the price" if selling else
                     f"Back to ${usd_start:,.2f}, the last buy's" if usd_start else "Back to 0"),
                    "Up / down $10")
    shares = AmountBox("shares", default_shares, shares_step,
                       (f"Back to all {free or 0:,} owned" + (f" (less {in_orders:,} in sell orders)" if in_orders else "")
                        if selling else "Back to the shares the default $ buys at the price"),
                       "Up / down 1,000 shares under $1, else 10")
    # the quick prices: a click puts it in the price box
    quick = QWidget()
    qlay = QHBoxLayout(quick)
    qlay.setContentsMargins(0, 0, 0, 0)
    qlay.setSpacing(4)
    quick_buttons = {}
    for key, name, color in (("bid", "Bid", "#1c6e3a"), ("ask", "Ask", "#9c2a35"), ("current", "Current", "#4a5560"),
                             ("clicked", "Clicked", "#3b5f8a")):
        b = QPushButton(name)
        b.setStyleSheet(f"QPushButton {{ background: {color}; color: #ffffff; border: 1px solid {color}; border-radius: 3px;"
                        f" padding: 3px 8px; }} QPushButton:hover {{ background: {QColor(color).lighter(130).name()}; }}"
                        " QPushButton:disabled { background: #2a2f34; color: #6c7176; border-color: #2a2f34; }")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        qlay.addWidget(b)
        quick_buttons[key] = (b, name)
    qlay.addStretch()

    def quick_prices():
        q = dict(quotes() if quotes else {})
        q["clicked"] = start
        return q

    def refresh_quick():
        q = quick_prices()
        market = kind.currentData() == "MARKET"
        for key, (b, name) in quick_buttons.items():
            v = q.get(key)
            b.setText(f"{name} {fmt_price(v)}" if v else name)
            b.setEnabled(bool(v) and not market)
            b.setToolTip(f"The price: the {name.lower()} " + ("where the chart was right-clicked" if key == "clicked"
                                                               else "now") if v else f"No {name.lower()} known")

    def use_quick(key):
        v = quick_prices().get(key)
        if v:
            box.set_price(Decimal(str(v)))
    for key, (b, _n) in quick_buttons.items():
        b.clicked.connect(lambda _c=False, k=key: use_quick(k))
    usd.setToolTip("The amount in $ (before the commission): the shares follow it, as many as it buys")
    shares.setToolTip("The amount of shares: the $ follows it")
    total = QLabel()
    total_name = "$ less the commission" if selling else "$ with the commission"
    aon = aon_box()
    rows = [("Ticker", ticker), ("Order", (side, BUY_COLOR if side == "BUY" else SELL_COLOR)),
            ("Type", kind), ("Duration", duration), ("Fill", aon), ("Quick price", quick), ("Price", PRICE_ROW), ("Amount in $", usd),
            ("Shares", shares), (total_name + (" (usual)" if usual else ""), total)]
    if selling:
        rows.insert(2, ("Owned", "-" if owned is None else f"{owned:,}" + (f" ({in_orders:,} in sell orders)"
                                                                            if in_orders else "")))
    if note:
        rows.append(("Note", (note, "#f0a020")))
    dlg, title_label, _values, send = _order_dialog(parent, account_name, f"{side} {ticker}", rows,
                                                    side.title(), BUY_COLOR if side == "BUY" else SELL_COLOR, box)
    state = {"last": None, "busy": False}

    def update():
        p, n = price(), int(shares.value())
        market = kind.currentData() == "MARKET"
        send.setEnabled(n > 0 and (p > 0 or market))
        amount = n * p
        taken = sell_fee(fee, amount) if selling else fee       # (a sale's: never more than it brings)
        net = amount - taken if selling else amount + taken
        sign = "-" if selling else "+"
        about = "≈ " if market else ""
        total.setText(f"<b style='color:#e3b23c'>{about}${amount:,.2f} {sign} ${taken:.2f} = "
                      f"{'-' if net < 0 else ''}${abs(net):,.2f}</b>")
        total.setToolTip("At the price above: a market order's price is the market's when it fills" if market else "")
        what = f"{side} {n:,} {ticker} " + ("at the market" if market else f"at {fmt_price(p)}" if p else "")
        title_label.setText(f"<b>{what.strip()}?</b>" if n else f"<b>{side} {ticker}</b>")
        dlg.setWindowTitle(what.strip() if n else f"{side} {ticker}")

    def sync(source):
        """The one typed last stays (the $ or the shares), the other follows it; both at their
        defaults (white) until either is typed over."""
        if state["busy"]:
            return
        state["busy"] = True
        if source in ("usd", "shares"):
            state["last"] = source
        p = price()
        usd.refresh()
        shares.refresh()
        if state["last"] == "usd":
            if usd.edit.text() and p > 0:
                shares.set_value(int(usd.value() / p + 1e-9))
            elif not usd.edit.text():
                shares.edit.clear()
        elif state["last"] == "shares":
            if shares.edit.text():
                usd.set_value(int(shares.value()) * p)
            else:
                usd.edit.clear()
        state["busy"] = False
        update()

    def kind_changed(*_):
        market = kind.currentData() == "MARKET"
        box.setEnabled(not market)
        if market:
            duration.setCurrentIndex(duration.findData("DAY"))
            aon.setChecked(False)
        duration.setEnabled(not market)
        aon.setEnabled(not market)          # (All or none: limit orders)
        refresh_quick()
        update()
    usd.value_changed.connect(lambda _v: sync("usd"))
    shares.value_changed.connect(lambda _v: sync("shares"))
    box.price_changed.connect(lambda _v: sync("price"))
    kind.currentIndexChanged.connect(kind_changed)
    refresh_quick()
    quick_timer = QTimer(dlg)
    quick_timer.timeout.connect(refresh_quick)
    quick_timer.start(1000)
    update()
    first = (shares if selling else usd).edit
    QTimer.singleShot(0, lambda: first.setFocus(Qt.FocusReason.OtherFocusReason))     # once it shows
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    if not accepted:
        return None
    for b in (box, usd, shares):
        b._steps()
    market = kind.currentData() == "MARKET"
    p, n = price(), int(shares.value())
    if n <= 0 or (not market and p <= 0):
        return None
    if remember_usd and usd.value() > 0:
        remember_usd(round(usd.value(), 2))     # the buy's default $ next time
    return {"shares": n, "price": None if market else p, "type": kind.currentData(),
            "duration": "DAY" if market else duration.currentData(), "aon": not market and aon.isChecked()}


STEP_GREEN, STEP_YELLOW, STEP_GRAY = "#1db31d", "#e3b23c", "#5a6068"
STEP_R, STEP_GAP = 8, 26        # the circles' radius, the distance between their centres


def paint_steps(p, rect, steps, r=STEP_R, gap=STEP_GAP):
    """An ATM trade's 4 steps in rect: circles joined by a line, a green ✔ in each step done, a yellow
    half for one partly done, empty gray for the rest (steps: [DONE / HALF / TODO] x 4)."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QPainter, QPainterPath, QPen
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    x0 = rect.left() + (rect.width() - (2 * r + 3 * gap)) / 2 + r
    y = rect.center().y() + 0.5
    centres = [QPointF(x0 + i * gap, y) for i in range(4)]
    for i in range(3):          # the joining lines: green between steps done
        color = STEP_GREEN if steps[i + 1] in (DONE, HALF) and steps[i] == DONE else STEP_GRAY
        p.setPen(QPen(QColor(color), 2))
        p.drawLine(QPointF(centres[i].x() + r, y), QPointF(centres[i + 1].x() - r, y))
    for c, st in zip(centres, steps):
        circle = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        if st == DONE:
            p.setPen(QPen(QColor(STEP_GREEN), 1.5))
            p.setBrush(QColor(STEP_GREEN))
            p.drawEllipse(circle)
            tick = QPainterPath(QPointF(c.x() - r * 0.45, c.y()))
            tick.lineTo(QPointF(c.x() - r * 0.1, c.y() + r * 0.38))
            tick.lineTo(QPointF(c.x() + r * 0.5, c.y() - r * 0.4))
            p.setPen(QPen(QColor("#ffffff"), 2, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tick)
        elif st == HALF:            # the left half filled, yellow
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(STEP_YELLOW))
            p.drawPie(circle, 90 * 16, 180 * 16)
            p.setPen(QPen(QColor(STEP_YELLOW), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(circle)
        else:
            p.setPen(QPen(QColor(STEP_GRAY), 1.5))
            p.setBrush(QColor("#1b1e22"))
            p.drawEllipse(circle)
    p.restore()


class StepsView(QWidget):
    """The 4 steps of the checklist's ATM trade, beside its title (hidden without a trade)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.steps = None
        self.setFixedSize(2 * STEP_R + 3 * STEP_GAP + 8, 2 * STEP_R + 6)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.hide()

    def set_trade(self, e):
        steps = trade_steps(e) if e else None
        if steps != self.steps:
            self.steps = steps
            words = {DONE: "✔", HALF: "partly", "todo": "not yet"}
            self.setToolTip("\n".join(f"{i + 1}. {n}: {words[st]}" for i, (n, st) in enumerate(zip(STEP_NAMES, steps)))
                            if steps else "")
            self.update()
        self.setVisible(steps is not None)

    def paintEvent(self, _event):
        if self.steps:
            from PyQt6.QtGui import QPainter
            p = QPainter(self)
            paint_steps(p, self.rect(), self.steps)
            p.end()


def aon_box():
    """The order dialogs' "All or none" checkbox: unchecked each time (a choice for this order only)."""
    box = QCheckBox("All or none")
    box.setChecked(False)
    box.setToolTip("All or none (AON): filled all at once, or not at all: no partial fills (such as 300 of "
                   "125,000, whose commission costs more than they're worth).\nBut an AON order usually "
                   "isn't shown on Level II, and fills only when someone trades the whole amount at your price.")
    return box


def confirm_order(parent, account_name, title, rows, action, color=BUY_COLOR):
    """Asks before an order goes out. rows: [(label, value)], a value can be (text, its colour).
    account_name: the account it goes to, shown first for checking (it is chosen on the Schwab
    tab). color: the send button's. True when approved."""
    dlg = _order_dialog(parent, account_name, title, rows, action, color)[0]
    return dlg.exec() == QDialog.DialogCode.Accepted


def _order_dialog(parent, account_name, title, rows, action, color, price_box=None):
    """The order dialog: (dialog, its title label, the value labels of rows (None at PRICE_ROW),
    the send button)."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    title_label = QLabel(f"<b>{title}</b>")
    lay.addWidget(title_label)
    form = QGridLayout()
    form.setHorizontalSpacing(14)
    rows = [("Account", (account_name, ORDER_COLOR))] + rows
    values = []
    for i, (k, v) in enumerate(rows):
        form.addWidget(QLabel(f"<span style='color:#a8a8a8'>{k}</span>"), i, 0)
        if v is PRICE_ROW:
            form.addWidget(price_box, i, 1)
            values.append(None)
        elif isinstance(v, QWidget):        # a box of its own (the amount, the duration...)
            form.addWidget(v, i, 1)
            values.append(None)
        else:
            label = QLabel(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>")
            form.addWidget(label, i, 1)
            values.append(label)
    values = values[1:]         # the rows', without the account's
    form.itemAtPosition(0, 1).widget().setToolTip("Chosen on the Monitor tab")
    lay.addLayout(form)
    buttons = QHBoxLayout()
    buttons.addStretch()
    send, cancel = QPushButton(action), QPushButton("Cancel")
    button = ("QPushButton {{ background: {0}; color: #ffffff; font-weight: bold; border: 1px solid {0};"
              " border-radius: 4px; padding: 5px 16px; }} QPushButton:hover {{ background: {1}; }}")
    send.setStyleSheet(button.format(color, QColor(color).lighter(125).name()))
    cancel.setStyleSheet(button.format("#4a5560", "#5b6875"))
    cancel.setDefault(True)             # Enter doesn't send it by mistake
    send.setAutoDefault(False)
    send.clicked.connect(dlg.accept)
    cancel.clicked.connect(dlg.reject)
    buttons.addWidget(send)
    buttons.addWidget(cancel)
    lay.addSpacing(6)
    lay.addLayout(buttons)
    cancel.setFocus()
    if parent is not None:
        parent.dialog = dlg             # for tests
    return dlg, title_label, values, send


# ------------------------------------------------------------ ATM trades ---
class AtmTrades(QObject):
    """The ATM trades, kept in atm.json: each Buy ATM sent, with the plan and the market at that
    moment, and what happened since. state: "buy_open" (waiting for the buy) -> "bought" (you own
    it) -> "sell_open" (waiting for the sell) -> "sold". A buy canceled (or rejected, or expired)
    before anything was bought is "canceled". They all stay until cleared on the Monitor tab. The open orders are checked
    at Schwab every ORDER_CHECK_S seconds. Also keeps the latest market of the trades' tickers."""
    changed = pyqtSignal()          # the trades, or their states
    market_changed = pyqtSignal(str)    # a ticker's bid / ask / Level II
    order_news = pyqtSignal(dict)   # an order of an account filled (partly), canceled, placed...: see order_events
    buy_sent = pyqtSignal(str)      # a Buy ATM went out for this ticker

    def __init__(self, path: Path, settings: dict, save_settings, fee_reader=None, trader=None):
        super().__init__()
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.fee_reader, self.trader = fee_reader, trader
        self.fee_of = lambda ticker: None   # Schwab's fee for a ticker, if read (set by the panel)
        self.read_fee = lambda ticker: None     # asks Schwab for it, once a session (set by the panel)
        self.entries = self._read()
        self.sending = set()        # tickers with a buy on its way, entry ids with a sell on its way
        self.pending = {}           # the same keys -> what was sent
        self.canceling = set()      # entry ids whose order is being canceled
        self.market = {}            # ticker -> {"bid": [price, size], "ask": [...], "bids": [[mm, price, size]], "asks"}
        # the chart's live Bid / Ask and Level II, when it shows that ticker (the panel sets it): {"bid", "ask",
        # "bids", "asks"} or None
        self.chart_quote = lambda ticker: None
        self.local_quotes = self._read_quotes()     # order quotes without the shared data (order_quotes.json)
        self.local_actuals = self._read_json("order_actuals.json")      # ...and the orders' Actual$
        self.actuals_asked = {}     # account -> (time.time(), the order ids asked for): the transactions read
        self.schwab_orders = {}     # account tail -> its orders of the last 60 days, as order_info()
        self.orders_time = {}       # account tail -> when they were read (time.time())
        self.cancel_ids = {}        # key -> the order id the app is canceling (not a thinkorswim change)
        self.app_canceled = set()   # order ids the app canceled
        self.positions = {}         # account tail -> [position_info()]: the stocks owned, read with each check
        # With several computers, only the main server's app decides what an order's status does to
        # a trade (bought, sold, canceled, changed in thinkorswim); the others show what it decided
        # (the shared data), so two computers never race to write the same trade
        self.decides = lambda: True
        self._migrate()
        if trader:
            trader.placed.connect(self._placed)
            trader.checked.connect(self._checked)
            trader.canceled.connect(self._canceled)
            trader.quotes.connect(self._quotes)
            trader.orders.connect(self._orders)
            trader.transactions.connect(self._transactions_read)
            trader.replaced.connect(self._replaced)
            trader.positions.connect(self._positions)
            trader.sent.connect(self._sent)
            trader.order_quote.connect(self.keep_order_quote)
            trader.snapshot_of = self.snapshot_now
        # The timer ticks every ORDER_CHECK_S; tick() reads only what is due (see ORDER_CHECK_S)
        self.activity_live = False
        self.tab_open = False       # the Monitor tab is shown
        self.orders_checked = 0.0
        self.positions_checked = 0.0
        self.quotes_checked = 0.0
        self.event_timer = QTimer(self)
        self.event_timer.setSingleShot(True)
        self.event_timer.setInterval(ORDER_EVENT_DELAY_MS)
        self.event_timer.timeout.connect(lambda: self.check(quotes=False))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.timeout.connect(self.pl_tick)        # (a sold trade: Schwab's P/L)
        self.timer.start(ORDER_CHECK_S * 1000)
        QTimer.singleShot(3000, self.check)
        # a Sell Partial waits for its buy's rest to be canceled (here, or told by the shared data)
        self.changed.connect(self.partial_followup, Qt.ConnectionType.QueuedConnection)

    # ---- the file, or the shared data (the server's: use_shared) ----
    def use_shared(self, shared):
        """From now on the trades are the shared data's (atm/<account>/<trade id>), not atm.json."""
        self.shared = shared
        self.reload_shared()

    def reload_shared(self):
        """The trades as the shared data has them now (changed elsewhere, or a change refused)."""
        if getattr(self, "shared", None) is None:
            return
        # Each trade kept as the same object, updated in place: the Monitor tab's rows (rebuilt only
        # when what they show changes) hold their trade, and an old copy there would make Clear or a
        # new Sell-at price act on a trade that isn't in the list any more
        old = {e["id"]: e for e in self.entries}
        entries = []
        for e in self._fixed(list(self.shared.get("atm/").values())):
            same = old.get(e["id"])
            if same is not None:
                same.clear()
                same.update(e)
                e = same
            entries.append(e)
        self.entries = entries
        self.changed.emit()

    def shared_key(self, e):
        return f"{e.get('account') or self.chosen_account() or 'none'}/{e['id']}"

    def _read(self):
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return self._fixed(entries)

    @staticmethod
    def _fixed(entries):
        for e in entries:
            if "stop" in e:         # called "safety-net" since v0.9.4
                e["safety-net"] = e.pop("stop")
            e.setdefault("id", str(e.get("order_id") or f"{e.get('ticker')}-{e.get('time')}"))
            e.setdefault("state", "buy_open")
            e.setdefault("filled", 0)
            e.setdefault("sold", 0)
            e.setdefault("exit-live", e.get("exit"))
            e.setdefault("safety-net-live", e.get("safety-net"))
        return entries

    @staticmethod
    def keep_history(e):
        """The trade's note, when new, kept in its history with the time (the checklist doesn't show the
        notes: the history is for looking back, on hover)."""
        note = (e.get("note") or "").strip()
        history = e.setdefault("history", [])
        if note and (not history or history[-1].get("text") != note):
            history.append({"at": datetime.now().isoformat(timespec="seconds"), "text": note})
            del history[:-HISTORY_MAX]

    @staticmethod
    def history_text(e, lines=20):
        """The trade's history as text, newest first ("" when none)."""
        rows = [f"{h.get('at', '')[5:16].replace('T', ' ')}  {h.get('text', '')}"
                for h in reversed(e.get("history") or [])][:lines]
        return "\n".join(rows)

    def save(self):
        for e in self.entries:
            self.keep_history(e)
        if getattr(self, "shared", None) is not None:
            # only the trades that changed go to the server, each from the version it had here
            self.shared.sync("atm/", {self.shared_key(e): e for e in self.entries})
            return
        text = json.dumps(self.entries, indent=1)
        # each [price, amount] / [market maker, price, amount] on one line
        text = re.sub(r"\[\s+([^\[\]{}]*?)\s+\]", lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", text)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            QMessageBox.warning(QApplication.activeWindow(), self.path.name, f"Can't save {self.path.name}: {e.strerror}")

    def _migrate(self):
        """Orders sent before atm.json kept them (settings["orders"], v0.9.0-0.9.2) become entries."""
        old = self.settings.pop("orders", None)
        if not old:
            return
        for ticker, o in old.items():
            if any(str(e.get("order_id")) == str(o.get("buy_id")) for e in self.entries):
                continue
            shares, entry = o.get("shares"), o.get("entry")
            e = {"id": str(o.get("buy_id") or f"{ticker}-{o.get('sent')}"), "order_id": o.get("buy_id"),
                 "ticker": ticker, "account": o.get("account"), "entry": entry, "exit": o.get("exit"),
                 "shares": shares, "usd": round(shares * entry, 2) if shares and entry else None,
                 "safety-net": o.get("stop"), "fee": None, "time": o.get("sent"),
                 "bid": [None, None], "ask": [None, None], "level2": {"bids": [], "asks": []},
                 "state": o.get("state", "buy_open"), "filled": o.get("filled", 0), "sold": 0,
                 "sell_at": o.get("sell_price") or o.get("exit"), "note": o.get("note", "")}
            for k in ("sell_id", "sell_price", "status", "bought"):
                if o.get(k) is not None:
                    e[k] = o[k]
            self.entries.append(e)
        self.save()
        self.save_settings(self.settings)

    # ---- looking up ----
    def active(self, ticker):
        """The ticker's trade the checklist shows: the last one not reset after it was sold."""
        for e in reversed(self.entries):
            if e.get("ticker") == ticker and not e.get("reset") and e.get("state") != "canceled":
                return e
        return None

    def find(self, eid):
        return next((e for e in self.entries if e["id"] == eid), None)

    def open_tickers(self):
        """The tickers with a buy or sell order working, or stocks owned."""
        return sorted({e["ticker"] for e in self.entries if e.get("state") not in ("sold", "canceled")})

    @staticmethod
    def remaining(e):
        """The stocks owned now."""
        return max(0, (e.get("filled") or 0) - (e.get("sold") or 0))

    def busy(self, e=None, ticker=None):
        """An order on its way to Schwab (from here, or a sell from Telegram: the main server's), or
        being canceled."""
        return (ticker in self.sending if ticker else False) or (
            e is not None and (e["id"] in self.sending or e["id"] in self.canceling or bool(e.get("sending"))))

    def account_name(self, tail):
        return self.fee_reader.kind(tail) if self.fee_reader else f"...{tail}"

    @staticmethod
    def profit(e, bid=None):
        """$ after both fees: of the sale when sold, else what selling the rest at `bid` would give
        (None without a bid)."""
        fee = e.get("fee")
        fee = FEE_USD if fee is None else fee
        cost = (e.get("filled") or 0) * e["entry"]
        got = e.get("proceeds") or 0.0
        if e.get("state") == "sold":
            if (e.get("pl") or {}).get("net") is not None:      # Schwab's own
                return e["pl"]["net"]
            return got - cost - fee - sell_fee(fee, got)
        if e.get("state") == "canceled" or not e.get("filled") or bid is None:
            return None
        got += AtmTrades.remaining(e) * bid
        return got - cost - fee - sell_fee(fee, got)

    # ---- orders ----
    def send_buy(self, snapshot):
        """Sends the Buy ATM: snapshot is the entry-to-be (the plan and the market now)."""
        ticker = snapshot["ticker"]
        self.sending.add(ticker)
        self.pending[ticker] = snapshot
        self.changed.emit()
        self.trader.place(ticker, ticker, "BUY", snapshot["shares"], snapshot["entry"], snapshot["account"],
                          bool(snapshot.get("aon")))

    def ask_sell_partial(self, parent, e, price, fee=None):
        """! Sell Partial (a buy partly filled): the dialog, then the rest of the buy canceled; once Schwab
        says so (the trade "bought"), the sell goes out by itself (partial_followup)."""
        if self.trader is None or e.get("state") != "buy_open" or not e.get("filled") or self.busy(e) \
                or e.get("partial_sell"):
            return
        if not e.get("order_id"):
            QMessageBox.warning(parent, "Sell Partial", "The buy order's number isn't known. Cancel it in Schwab.")
            return
        if self.fee_reader:
            self.fee_reader.load_accounts()
        got = partial_sell_dialog(parent, self.account_name(e.get("account")), e, price,
                                  e.get("fee") if fee is None else fee)
        if got is None or e.get("state") != "buy_open" or self.busy(e):
            return
        shares, price, aon = got
        e["partial_sell"] = {"shares": shares, "all": shares >= int(e.get("filled") or 0), "price": price,
                             "aon": aon, "by": socket.gethostname(), "at": time.time()}
        e["note"] = f"Sell Partial: canceling the rest of the buy, then selling {shares:,} @ {fmt_price(price)}..."
        self.save()
        self.canceling.add(e["id"])
        self.cancel_ids[e["id"]] = str(e["order_id"])
        self.app_canceled.add(str(e["order_id"]))
        self.changed.emit()
        self.trader.cancel(e["id"], "BUY", e["order_id"], e.get("account"))

    def partial_followup(self):
        """A Sell Partial waiting (sent from this computer): its rest of the buy canceled, the trade now
        "bought": its sell goes out (all the stocks bought, if all were chosen and more came meanwhile)."""
        if self.trader is None:
            return
        me, now = socket.gethostname(), time.time()

        def stale(want):
            """Approved too long ago (or before its time was kept): not sent at a price chosen then."""
            return now - float(want.get("at") or 0) > FOLLOWUP_MAX_S
        for e in self.entries:
            again = e.get("rebuy")
            # the rest canceled: "ready" (this version's decision), or "bought" (an app of an older version
            # decided it, or a race): either way the new buy goes out
            done = again and (again.get("ready") and e.get("state") == "buy_open" or e.get("state") == "bought")
            if again and again.get("by") == me and done and not self.busy(e):
                if stale(again):
                    e.pop("rebuy", None)
                    if e.get("state") == "buy_open":
                        e["state"] = "bought"
                    e["note"] = (f"Update Buy: the rest was canceled, but the new buy of {e['ticker']} wasn't sent "
                                 f"(approved more than {FOLLOWUP_MAX_S // 60} minutes ago: its price may be old). "
                                 f"You own {self.remaining(e):,}: buy more with a new order if you still want them.")
                    self.save()
                    self.changed.emit()
                    continue
                rest = int(again["shares"]) - int(e.get("filled") or 0)
                if rest <= 0:           # (all were bought before the cancel)
                    e.pop("rebuy", None)
                    e.update(state="bought", note="Update Buy: all of them were bought before the cancel.")
                    self.save()
                    self.changed.emit()
                    continue
                if not self.first_time(e, "rebuy", again):
                    continue
                self.sending.add(e["id"])
                self.pending[e["id"]] = {"rebuy": True, "shares": int(again["shares"]), "price": float(again["price"]),
                                         "size": again.get("size"), "aon": bool(again.get("aon")),
                                         "old_id": str(e.get("order_id") or "")}
                self.changed.emit()
                self.trader.place(e["id"], e["ticker"], "BUY", rest, float(again["price"]), e.get("account"),
                                  bool(again.get("aon")))
                continue
            again = e.get("resell")
            if again and again.get("by") == me and e.get("state") != "sell_open" and not self.busy(e):
                e.pop("resell", None)
                if not self.first_time(e, "resell", again):
                    continue
                left = self.remaining(e)
                if stale(again) or e.get("state") != "bought" or left <= 0:
                    e["note"] = ("Update Sell: the rest of the order was canceled, but the new sell wasn't sent ("
                                 + (f"approved more than {FOLLOWUP_MAX_S // 60} minutes ago: its price may be old"
                                    if stale(again) else "nothing left to sell" if left <= 0 else
                                    "the trade isn't waiting for a sell") + "). Sell ATM sends it.")
                    self.save()
                    self.changed.emit()
                    continue
                self.place_sell(e, left, float(again["price"]), bool(again.get("aon")))
                continue
            want = e.get("partial_sell")
            if not want or want.get("by") != me or e.get("state") == "buy_open":
                continue
            e.pop("partial_sell", None)
            if not self.first_time(e, "partial_sell", want):
                continue
            if stale(want):
                e["note"] = (f"Sell Partial: the rest of the buy was canceled, but the sell wasn't sent (approved more "
                             f"than {FOLLOWUP_MAX_S // 60} minutes ago: its price may be old). Sell ATM sends it.")
                self.save()
                self.changed.emit()
                continue
            left = self.remaining(e)
            if e.get("state") != "bought" or left <= 0 or self.busy(e):
                e["note"] = "Sell Partial: not sent (the trade isn't waiting for a sell)."
                self.save()
                self.changed.emit()
                continue
            shares = left if want.get("all") else min(int(want["shares"]), left)
            self.place_sell(e, shares, float(want["price"]), bool(want.get("aon")))

    def first_time(self, e, kind, want):
        """A follow-up order's request (rebuy / resell / partial_sell, taken off the trade): True the first time
        here - saved at once without it, so no copy of the trade brings it back -, False after (already sent:
        nothing again)."""
        done = self.__dict__.setdefault("followed", set())
        key = (str(e["id"]), kind, str(want.get("at")), str(want.get("price")))
        if key in done:
            if e.pop(kind, None) is not None:
                self.save()
            return False
        done.add(key)
        if kind != "rebuy":         # (the rebuy's request stays until its order is placed: its own step)
            self.save()
        return True

    def place_sell(self, e, shares, price, aon=False):
        """The trade's sell order out (approved already)."""
        self.sending.add(e["id"])
        self.pending[e["id"]] = {"sell_price": price, "shares": shares, "aon": aon}
        self.changed.emit()
        self.trader.place(e["id"], e["ticker"], "SELL", shares, price, e.get("account"), aon)

    def ask_sell(self, parent, e, price, fee=None, on_bid=None):
        """Sells all the stocks owned at `price`, GTC, after the dialog is approved.
        on_bid: (the price row's name, how it was found) for Sell on Bid."""
        if self.trader is None or e.get("state") != "bought" or self.busy(e):
            return
        if not price:
            QMessageBox.warning(parent, "Sell ATM", "There's no exit price.")
            return
        shares = self.remaining(e)
        total = shares * price
        fee = e.get("fee") if fee is None else fee
        rows = [("Ticker", e["ticker"]), ("Order", "SELL · Limit · GTC (good till canceled)"),
                ("Amount of stocks", f"{shares:,} (all)"),
                (on_bid[0] if on_bid else "Limit price (exit)", fmt_price(price))
                ] + ([("On the Bid", on_bid[1])] if on_bid else []) + fee_rows(fee, total, sell=True)
        used = FEE_USD if fee is None else fee
        got = (e.get("proceeds") or 0) + total
        gain = got - (e.get("filled") or 0) * e["entry"] - used - sell_fee(used, got)
        rows.append(("Profit after both fees", usd_signed(gain)))
        aon = aon_box()
        rows.append(("Fill", aon))
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(e.get("account")), f"Sell {shares:,} {e['ticker']} at "
                             f"{fmt_price(price)}?", rows, "Send SELL order", SELL_COLOR):
            return
        self.place_sell(e, shares, price, aon.isChecked())

    def ask_cancel(self, parent, e, side):
        """Cancels the trade's open buy or sell order, after asking."""
        if self.trader is None or e is None or self.busy(e):
            return
        if side == "BUY" and e.get("state") == "buy_open":
            order_id, text = e.get("order_id"), (f"Cancel the buy order of {e['shares']:,} {e['ticker']} "
                                                 f"at {fmt_price(e['entry'])}?")
        elif side == "SELL" and e.get("state") == "sell_open":
            order_id, text = e.get("sell_id"), (f"Cancel the sell order of {self.remaining(e):,} {e['ticker']} "
                                                f"at {fmt_price(e['sell_price'])}?\nYou keep the stocks; "
                                                f"Sell ATM can send it again.")
        else:
            return
        if not order_id:
            QMessageBox.warning(parent, "Cancel", "The order's number isn't known. Cancel it in Schwab.")
            return
        answer = QMessageBox.question(parent, f"Cancel {side.title()}", text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.canceling.add(e["id"])
        self.cancel_ids[e["id"]] = str(order_id)
        self.app_canceled.add(str(order_id))
        self.changed.emit()
        self.trader.cancel(e["id"], side, order_id, e.get("account"))

    # ---- the account's other orders (sent from thinkorswim), and changing orders from the chart ----
    def chosen_account(self):
        return self.settings.get("schwab_account") or ""

    def all_accounts(self):
        """The login's accounts (last 3 digits), once read."""
        return [t for t, _, _ in getattr(self.trader.session, "accounts", [])] if self.trader else []

    def tracked_tickers(self):
        """The tickers to stream and record: the open ATM trades', and every account's open orders'
        and stocks owned (as last read), in that order."""
        out = list(self.open_tickers())
        for account in sorted(self.schwab_orders):
            out += [o["ticker"] for o in self.schwab_orders[account] if o.get("open")]
        for account in sorted(self.positions):
            out += [p["ticker"] for p in self.positions[account]]
        return list(dict.fromkeys(t for t in out if t))

    def adopt_outside_orders(self, account, orders):
        """Orders of an open ATM trade's ticker placed outside it (thinkorswim, the chart's or the portfolio's
        buttons), after it started, and no trade's: adopted, so the trade knows what it owns and sells.
        A buy done with fills: its shares added (the entry: the average); an open sell while bought: the trade's
        sell order (step 3); a sell done with fills: sold. Each one once (adopted_ids)."""
        tracked = self.tracked_ids()
        changed = False
        for e in self.entries:
            if (e.get("account") or self.chosen_account()) != account or e.get("state") not in ("bought", "sell_open"):
                continue
            changed = self.entry_from_fills(e, orders) or changed
            if e.get("state") == "bought":
                old = {str(x) for x in e.get("replaced_ids", [])}
                live = [o for o in orders if o["id"] in old and o.get("ticker") == e.get("ticker")
                        and o.get("side") == "SELL" and o.get("open")]
                if live:
                    o = max(live, key=lambda o: o.get("entered") or "")
                    e.update(state="sell_open", sell_id=o["id"], sell_price=o.get("price"),
                             sell_filled=int(o.get("filled") or 0), sell_shares=int(o.get("quantity") or 0),
                             sell_time=datetime.now().isoformat(timespec="seconds"))
                    e.pop("sell_end", None)
                    e["note"] = (f"Followed again: its sell order of {int(o.get('quantity') or 0):,} @ "
                                 f"{fmt_price(o.get('price'))} is still open in Schwab"
                                 + (f" ({int(o.get('filled') or 0):,} sold so far)." if o.get("filled") else "."))
                    changed = True
                    continue
            start, _ = self.trade_window(e)
            mine = []
            for o in orders:
                try:
                    when = datetime.strptime(o.get("entered") or "", "%Y-%m-%dT%H:%M:%S%z")
                except ValueError:
                    continue
                if o.get("ticker") == e.get("ticker") and o["id"] not in tracked and when >= start:
                    mine.append(o)
            for o in sorted(mine, key=lambda o: o.get("entered") or ""):
                got, price = int(o.get("filled") or 0), o.get("fill_price") or o.get("price") or 0
                what = None
                if o["side"] == "BUY" and not o.get("open") and got:
                    owned = int(e.get("filled") or 0)
                    e["entry"] = round((owned * e["entry"] + got * price) / (owned + got), 6)    # the average
                    e["filled"] = owned + got
                    e["shares"] = max(int(e.get("shares") or 0), e["filled"])
                    what = f"a buy of {got:,} @ {fmt_price(price)} placed outside the trade: added (the average entry)"
                elif o["side"] == "SELL" and o.get("open") and e.get("state") == "bought":
                    e.update(state="sell_open", sell_id=o["id"], sell_price=o.get("price"), sell_filled=got,
                             sell_shares=int(o.get("quantity") or 0), sell_time=datetime.now().isoformat(timespec="seconds"))
                    what = f"its sell order of {int(o.get('quantity') or 0):,} @ {fmt_price(o.get('price'))} " \
                           "(placed outside the trade) followed"
                elif o["side"] == "SELL" and not o.get("open") and got:
                    e["sold"] = int(e.get("sold") or 0) + got
                    e["proceeds"] = round((e.get("proceeds") or 0.0) + got * price, 6)
                    if self.remaining(e) <= 0:
                        e.update(state="sold", sold_time=datetime.now().isoformat(timespec="seconds"))
                    what = f"a sell of {got:,} @ {fmt_price(price)} done outside the trade: counted"
                if what:
                    e.setdefault("adopted_ids", []).append(o["id"])
                    tracked.add(o["id"])
                    e["note"] = f"Adopted {what}."
                    changed = True
        if changed:
            self.save()

    def entry_from_fills(self, e, orders):
        """A trade with a buy adopted (placed outside it): its entry checked against Schwab's fills - their average,
        when the list has the fills of every share it bought - and corrected (an adoption by v2.7.70 or older took
        a partly filled order's unfilled rest for shares bought at 0: FOBIF's entry 0.0124 for 0.0154). True when
        corrected."""
        adopted = {str(x) for x in e.get("adopted_ids") or []}
        ids = self.trade_order_ids(e)
        buys = [o for o in orders if o["id"] in ids and o.get("side") == "BUY" and int(o.get("filled") or 0)
                and o.get("fill_price")]
        if not any(o["id"] in adopted for o in buys):
            return False
        shares = sum(int(o["filled"]) for o in buys)
        if shares != int(e.get("filled") or 0) or not e.get("entry"):
            return False
        avg = round(sum(int(o["filled"]) * float(o["fill_price"]) for o in buys) / shares, 6)
        if abs(avg - e["entry"]) <= avg * 0.005:
            return False
        e["note"] = (f"Entry corrected: {fmt_price(e['entry'])} -> {fmt_price(avg)} (Schwab's fills: "
                     f"{shares:,} bought in {len(buys)} order{'s' if len(buys) != 1 else ''}).")
        e["entry"] = avg
        return True

    def tracked_ids(self):
        """The order ids the ATM trades follow, now or before."""
        ids = set()
        for e in self.entries:
            for k in ("order_id", "sell_id"):
                if e.get(k):
                    ids.add(str(e[k]))
            if str(e.get("id", "")).isdigit():      # (a trade's id: its first buy order's)
                ids.add(str(e["id"]))
            ids.update(str(x) for x in e.get("replaced_ids", []))
            ids.update(str(x) for x in e.get("adopted_ids", []))
            # (its Schwab P/L's fills: every order it was made of, the old ones too)
            ids.update(str(f["order"]) for f in (e.get("pl") or {}).get("fills") or [] if f.get("order"))
        return ids

    @staticmethod
    def trade_order_ids(e):
        """The ids of the orders the trade was made of: its buys and sells, the replaced ones and the adopted
        ones, and those of its Schwab P/L's fills."""
        ids = {str(e[k]) for k in ("order_id", "sell_id") if e.get(k)}
        if str(e.get("id", "")).isdigit():          # (a trade's id: its first buy order's)
            ids.add(str(e["id"]))
        ids.update(str(x) for x in e.get("replaced_ids", []) + e.get("adopted_ids", []))
        ids.update(str(f["order"]) for f in (e.get("pl") or {}).get("fills") or [] if f.get("order"))
        return ids

    def trade_orders(self, e):
        """The trade's orders as Schwab's latest list of its account's orders has them, oldest first."""
        ids = self.trade_order_ids(e)
        found = [o for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), []) if o["id"] in ids]
        return sorted(found, key=lambda o: o.get("entered") or "")

    def other_orders(self, ticker=None, account=None):
        """The open limit orders of the ticker (None: of every ticker) in the account (the chosen
        one) that aren't an ATM trade's: sent from thinkorswim."""
        tracked = self.tracked_ids()
        return [o for o in self.schwab_orders.get(account or self.chosen_account(), [])
                if (ticker is None or o["ticker"] == ticker) and o["open"] and o["price"] and o["shares"] > 0
                and o["id"] not in tracked]

    def all_orders(self, account=None):
        """Every order of the account (the chosen one) of the last ORDERS_BACK_DAYS days, open or not, the ATM
        trades' too: each with "key" (its chart key: "atm:<trade id>:<side>" for an ATM trade's open order, else
        "schwab:<id>"), "busy" (what sending / canceling hold while it changes), "atm" (its open trade, or None)
        and "atm_order" (an ATM trade's, now or before)."""
        account = account or self.chosen_account()
        mine_ever = self.tracked_ids()
        mine = {}
        for e in self.entries:
            if (e.get("account") or self.chosen_account()) != account:
                continue
            if e.get("state") == "buy_open" and e.get("order_id"):
                mine[str(e["order_id"])] = (e, "BUY")
            elif e.get("state") == "sell_open" and e.get("sell_id"):
                mine[str(e["sell_id"])] = (e, "SELL")
        out = []
        for o in self.schwab_orders.get(account, []):
            e, side = mine.get(o["id"], (None, None))
            if o["open"] and e is not None and side == o["side"]:
                out.append(dict(o, key=f"atm:{e['id']}:{side}", busy=e["id"], atm=e, atm_order=True))
            else:
                key = f"schwab:{o['id']}"
                out.append(dict(o, key=key, busy=key, atm=None, atm_order=o["id"] in mine_ever))
        return out

    # ---- the stocks owned (the Monitor tab's list) ----
    def owned(self, account=None):
        """The stocks owned in the account (the chosen one), as last read: [position_info()]."""
        return self.positions.get(account or self.chosen_account(), [])

    def positions_read(self, account=None):
        return (account or self.chosen_account()) in self.positions

    def in_sell_orders(self, ticker, account=None):
        """The ticker's shares already in open sell orders in the account: others' and an ATM trade's."""
        account = account or self.chosen_account()
        n = sum(o["shares"] for o in self.other_orders(ticker, account) if o["side"] == "SELL")
        for e in self.entries:
            if e.get("ticker") == ticker and e.get("state") == "sell_open" and                     (e.get("account") or self.chosen_account()) == account:
                n += self.remaining(e) - (e.get("sell_filled") or 0)
        return n

    def bid_for(self, ticker, shares):
        """Selling `shares` on the bid: (the price the Level II bids fill them all at, best first; or the
        best bid without a book, a note when that differs from the best bid or isn't enough)."""
        m = self.market.get(ticker, {})
        best, best_size = (m.get("bid") or [None, None])[:2]
        bids = [b for b in (m.get("bids") or []) if b[1] and b[2]]
        if not bids:
            if best and best_size and best_size < shares:
                return best, (f"The bid {fmt_price(best)} is for {best_size:,} only, of the {shares:,} "
                              "(no Level II to look further down): the rest may not sell at it.")
            return best, ""
        price, total = bid_fill(bids, shares)
        if total < shares:
            return price, (f"All the bids down to the lowest ({fmt_price(price)}) add up to {total:,} only, "
                           f"of the {shares:,}: the rest may not sell.")
        if best and price < best:
            top = sum(b[2] for b in bids if b[1] >= best)
            return price, (f"The best bid {fmt_price(best)} takes {top:,} only, of the {shares:,}: "
                           f"{fmt_price(price)} is the bid that fills them all.")
        return price, ""

    def ask_sell_position(self, parent, ticker, where="bid"):
        """Sell on the Bid / Ask of the stocks owned list: the dialog (amount, price, duration), then
        a limit sell order (not an ATM trade: it shows with the other orders)."""
        if self.trader is None:
            return
        key = f"sell:{ticker}"
        if key in self.sending:
            return
        pos = next((p for p in self.owned() if p["ticker"] == ticker), None)
        if pos is None:
            return
        in_orders = self.in_sell_orders(ticker)
        if in_orders >= pos["shares"]:
            QMessageBox.information(parent, f"Sell {ticker}", f"All {pos['shares']:,} {ticker} are in sell orders "
                                    "already: change or cancel them in the All Orders list.")
            return
        m = self.market.get(ticker, {})
        free = pos["shares"] - in_orders
        filled_note = ""
        if where == "bid":          # the bid that takes them all (Level II), not only the best one's size
            start, filled_note = self.bid_for(ticker, free)
        else:
            start = (m.get(where) or [None])[0]
        start = start or (m.get("bid") or [None])[0] or pos.get("avg")
        if not start:
            QMessageBox.warning(parent, f"Sell {ticker}", f"No {where} for {ticker} yet: try again in a moment.")
            return
        self.read_fee(ticker)
        atm = next((e for e in self.entries if e.get("ticker") == ticker and e.get("state") == "bought"
                    and (e.get("account") or self.chosen_account()) == self.chosen_account()), None)
        note = (f"{self.remaining(atm):,} of these are an ATM trade's: Sell ATM sells them as the ATM trade."
                if atm else "")
        note = " ".join(x for x in (filled_note, note) if x)
        if self.fee_reader:
            self.fee_reader.load_accounts()
        res = sell_dialog(parent, self.account_name(self.chosen_account()), ticker, pos["shares"], in_orders,
                          start, self.fee_of(ticker), where.title(), note)
        if res is None:
            return
        shares, price, duration, aon = res
        self.sending.add(key)
        self.changed.emit()
        self.trader.send(key, ticker, "SELL", shares, price, self.chosen_account(), duration, "LIMIT", aon)

    def ask_order(self, parent, ticker, side, price, quotes=None):
        """The chart's BUY / SELL: the order dialog, then a limit or market order of the account
        chosen on the Monitor tab (not an ATM trade: it shows with the other orders). quotes(): the
        chart's {"bid", "ask", "current"} for the quick price buttons (the bid / ask read here when
        the chart has none)."""
        if not ticker:
            return
        if self.trader is None:
            QMessageBox.warning(parent, f"{side} {ticker}", "No Schwab login: orders can't be sent.")
            return
        key = f"{side.lower()}:{ticker}"
        if key in self.sending:
            QMessageBox.information(parent, f"{side} {ticker}", f"A {side} order of {ticker} is being sent: "
                                    "wait for it a moment.")
            return
        self.read_fee(ticker)
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not self.chosen_account():
            QMessageBox.warning(parent, f"{side} {ticker}", "Choose the account the orders go to first "
                                "(next to the tabs).")
            return
        owned, in_orders = None, 0
        if side == "SELL" and self.chosen_account() in self.positions:
            pos = next((p for p in self.owned() if p["ticker"] == ticker), None)
            owned = pos["shares"] if pos else 0
            in_orders = self.in_sell_orders(ticker) if pos else 0
        e = self.active(ticker)
        note = ""
        if e and (e.get("account") or self.chosen_account()) == self.chosen_account():
            note = f"{ticker} has an ATM trade: this order is separate from it."
        if quotes is not None and not (self.market.get(ticker, {}).get("bid") or quotes().get("bid")):
            self.trader.read_quotes([ticker])       # the bid and ask, for the quick prices

        def now():
            q = dict(quotes() if quotes else {})
            m = self.market.get(ticker, {})
            for k in ("bid", "ask"):
                q[k] = q.get(k) or (m.get(k) or [None])[0]
            return q
        res = order_dialog(parent, self.account_name(self.chosen_account()), ticker, side, price,
                           self.fee_of(ticker), owned, in_orders, note,
                           self.settings.get("order_usd_buy", 0.0) if side == "BUY" else 0.0,
                           self._remember_usd(side) if side == "BUY" else None, now)
        if res is None:
            return
        self.sending.add(key)
        self.changed.emit()
        self.trader.send(key, ticker, side, res["shares"], res["price"], self.chosen_account(), res["duration"],
                         res["type"], res.get("aon", False))

    def _remember_usd(self, side):
        """Keeps the $ typed in the chart's BUY / SELL dialog: that dialog starts with it next time."""
        def keep(usd):
            key = f"order_usd_{side.lower()}"
            if self.settings.get(key) != usd:
                self.settings[key] = usd
                self.save_settings(self.settings)
        return keep

    def _positions(self, account, positions, error):
        if positions is None:
            return              # tried again at the next check
        self.positions[account] = positions
        if account == self.chosen_account() and self.trader is not None:
            new = sorted({p["ticker"] for p in positions} - set(self.market))
            if new:
                self.trader.read_quotes(new)
        self.changed.emit()

    def _sent(self, key, order_id, message):
        self.sending.discard(key)
        if order_id is None and message:
            side, _, ticker = key.partition(":")
            QMessageBox.warning(QApplication.activeWindow(), f"{ticker} {side} order not sent", message)
        self.changed.emit()
        QTimer.singleShot(1500, self.check)     # the new order in the All Orders list

    # ---- the market when each order was sent (Bid / Ask then, and the Level II then) ----
    def snapshot_now(self, ticker):
        """{"time", "bid": [price, size], "ask": [...], "level2": {"bids": [[mm, price, size]], "asks"}}: the chart's
        live quote when it shows the ticker (the fullest), else the trades' stream / last read."""
        chart = self.chart_quote(ticker) or {}
        m = self.market.get(ticker, {})
        pair = lambda v: list(v) if v and v[0] else [None, None]
        return {"time": datetime.now().isoformat(timespec="seconds"), "ticker": ticker,
                "bid": pair(chart.get("bid") or m.get("bid")), "ask": pair(chart.get("ask") or m.get("ask")),
                "level2": {"bids": [list(r[:3]) for r in (chart.get("bids") or m.get("bids") or [])],
                           "asks": [list(r[:3]) for r in (chart.get("asks") or m.get("asks") or [])]}}

    def _quotes_path(self):
        return self.path.with_name("order_quotes.json")

    def _read_json(self, name):
        try:
            return json.loads(self.path.with_name(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ---- the orders' Actual$: Schwab's own (its transactions), kept once read ----
    def order_actual(self, account, order_id):
        """What Schwab charged for the order's fills: {"shares", "value", "fee", "fills", "days", "time"}, or None
        (not read yet)."""
        key = f"{account}/{order_id}"
        if getattr(self, "shared", None) is not None:
            item = self.shared.items.get("order_actuals/" + key)
            return item[1] if item else None
        return self.local_actuals.get(key)

    def want_actuals(self, account, orders):
        """(the main server's app) The filled orders whose Actual$ isn't kept yet (or has fewer shares than filled
        now): the account's transactions read for them, from the oldest one's day; not again too soon."""
        if self.trader is None or not self.decides():
            return
        need = [o for o in orders if int(o.get("filled") or 0)
                and int((self.order_actual(account, o["id"]) or {}).get("shares") or 0) < int(o["filled"])]
        if not need:
            return
        ids = sorted(o["id"] for o in need)
        at, asked = self.actuals_asked.get(account, (0.0, []))
        wait = ACTUALS_AGAIN_S if ids == asked else ACTUALS_READ_S
        if time.time() - at < wait:
            return
        self.actuals_asked[account] = (time.time(), ids)
        days = [datetime.strptime(o["entered"][:19], "%Y-%m-%dT%H:%M:%S") for o in need if o.get("entered")]
        now = datetime.now(timezone.utc)
        start = (min(days).replace(tzinfo=timezone.utc) - timedelta(days=1)) if days else now - timedelta(days=30)
        start = max(start, now - timedelta(days=ORDER_QUOTES_DAYS))
        self.trader.read_transactions("actuals:" + account, account, None, start, now)

    def keep_actuals(self, account, fills):
        """Schwab's fills of the account as each order's Actual$ (its shares, their value, the fees), kept for
        every computer (the shared data), ORDER_QUOTES_DAYS days."""
        by_order = {}
        for f in fills:
            if f.get("order"):
                by_order.setdefault(f["order"], []).append(f)
        from otc_orders import market_day
        cutoff = (datetime.now() - timedelta(days=ORDER_QUOTES_DAYS)).isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        changed = False
        for oid, fs in by_order.items():
            actual = {"ticker": fs[0].get("ticker"), "side": fs[0].get("side"),
                      "shares": sum(int(f.get("shares") or 0) for f in fs),
                      "value": round(sum(f.get("value") or 0 for f in fs), 4),
                      "fee": round(sum(f.get("fee") or 0 for f in fs), 2), "fills": len(fs),
                      "charged": sum(1 for f in fs if (f.get("fee") or 0) > 0.0049),
                      "days": sorted({market_day(f.get("time")) for f in fs} - {""}), "time": now}
            old = self.order_actual(account, oid)
            if old and {k: v for k, v in old.items() if k != "time"} == {k: v for k, v in actual.items() if k != "time"}:
                continue
            changed = True
            if getattr(self, "shared", None) is not None:
                self.shared.put(f"order_actuals/{account}/{oid}", actual)
            else:
                self.local_actuals[f"{account}/{oid}"] = actual
        if getattr(self, "shared", None) is not None:
            old = [k for k, v in self.shared.get("order_actuals/").items() if (v or {}).get("time", "") < cutoff]
            if old:
                self.shared.sync_items({}, ["order_actuals/" + k for k in old])
        elif changed:
            self.local_actuals = {k: v for k, v in self.local_actuals.items() if v.get("time", "") >= cutoff}
            try:
                path = self.path.with_name("order_actuals.json")
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.local_actuals), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass
        if changed:
            self.changed.emit()

    def _read_quotes(self):
        try:
            return json.loads(self._quotes_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def order_quote(self, account, order_id):
        """The market when the order was sent (snapshot_now()), or None (sent before they were kept)."""
        key = f"{account}/{order_id}"
        if getattr(self, "shared", None) is not None:
            item = self.shared.items.get("order_quotes/" + key)       # (direct: per row, at each rebuild)
            return item[1] if item else None
        return self.local_quotes.get(key)

    def keep_order_quote(self, account, order_id, snap, seen=False):
        """Keeps an order's market when it was sent (seen: first seen in the account's orders, a few seconds after it
        was placed outside the app); once per order. Those older than ORDER_QUOTES_DAYS go."""
        if not account or not order_id or snap is None or self.order_quote(account, order_id) is not None:
            return
        snap = dict(snap, seen=True) if seen else snap
        cutoff = (datetime.now() - timedelta(days=ORDER_QUOTES_DAYS)).isoformat()
        if getattr(self, "shared", None) is not None:
            self.shared.put(f"order_quotes/{account}/{order_id}", snap)
            old = [k for k, v in self.shared.get("order_quotes/").items() if (v or {}).get("time", "") < cutoff]
            if old:
                self.shared.sync_items({}, ["order_quotes/" + k for k in old])
        else:
            self.local_quotes[f"{account}/{order_id}"] = snap
            self.local_quotes = {k: v for k, v in self.local_quotes.items() if v.get("time", "") >= cutoff}
            try:
                tmp = self._quotes_path().with_suffix(".tmp")
                tmp.write_text(json.dumps(self.local_quotes), encoding="utf-8")
                tmp.replace(self._quotes_path())
            except OSError:
                pass
        self.changed.emit()

    def order_events(self, account, before, now):
        """What changed between two reads of the account's orders: [{"kind": "filled" | "partial" | "canceled" |
        "rejected" | "expired" | "replaced" | "placed", "account", "id", "ticker", "side", "shares" (this time's),
        "filled", "quantity", "price" (the limit), "fill_price", "atm" (an ATM trade's order)}]."""
        old = {o["id"]: o for o in before}
        mine = self.tracked_ids()
        out = []
        for o in now:
            p = old.get(o["id"])
            base = {"account": account, "id": o["id"], "ticker": o["ticker"], "side": o["side"],
                    "filled": o.get("filled") or 0, "quantity": o.get("quantity") or 0, "price": o.get("price"),
                    "fill_price": o.get("fill_price"), "atm": o["id"] in mine}
            was = (p or {}).get("filled") or 0
            if (o.get("filled") or 0) > was:
                out.append(dict(base, kind="filled" if o.get("status") == "FILLED" else "partial",
                                shares=(o.get("filled") or 0) - was))
            status, old_status = o.get("status"), (p or {}).get("status")
            if p is None and o.get("open"):
                out.append(dict(base, kind="placed", shares=o.get("shares") or 0))
            elif status != old_status and status in ("CANCELED", "REJECTED", "EXPIRED", "REPLACED") \
                    and (p is None or p.get("open")):
                out.append(dict(base, kind=status.lower(), shares=o.get("shares") or 0))
        return out

    def outside_sell(self, e):
        """A bought trade's ticker's open sell order, placed after it started and no trade's (outside it), or None."""
        start, _ = self.trade_window(e)
        tracked = self.tracked_ids()
        for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), []):
            if o.get("ticker") != e.get("ticker") or o.get("side") != "SELL" or not o.get("open") or o["id"] in tracked:
                continue
            try:
                if datetime.strptime(o.get("entered") or "", "%Y-%m-%dT%H:%M:%S%z") >= start:
                    return o
            except ValueError:
                continue
        return None

    def live_view(self, e):
        """The trade as Schwab's latest list of its account's orders shows it, before the main server's app
        has updated it: the shares bought / sold so far, and its step once the order is filled or ended (for
        the step circles). The trade itself (atm.json) isn't changed."""
        if e and e.get("state") == "bought":
            # its ticker's sell order placed outside it (the main server's app adopts it: shown so at once)
            outside = self.outside_sell(e)
            if outside is not None:
                return dict(e, state="sell_open", sell_id=outside["id"], sell_price=outside.get("price"),
                            sell_filled=int(outside.get("filled") or 0))
        o = self.live_order(e) if e else None
        if o is None:
            return e
        v, status = dict(e), o.get("status")
        if e.get("state") == "buy_open":
            v["filled"] = max(int(e.get("filled") or 0), int(e.get("filled_prev") or 0) + int(o.get("filled") or 0))
            if status == "FILLED" or (status in ORDER_DONE and v["filled"]):
                v["state"] = "bought"
            elif status in ORDER_DONE:
                v["state"] = "canceled"
        elif e.get("state") == "sell_open":
            v["sell_filled"] = max(int(e.get("sell_filled") or 0), int(o.get("filled") or 0))
            if status in ORDER_DONE:
                v["sold"] = int(e.get("sold") or 0) + v["sell_filled"]
                v["state"] = "sold" if self.remaining(v) <= 0 else "bought"
        return v

    # ---- a sold trade's P/L: Schwab's own (its transactions), else estimated from its orders ----
    @staticmethod
    def trade_window(e):
        """(from, to) of the trade's fills, as UTC datetimes: its buy sent (a minute before) to its sale (an hour after)
        or now."""
        def utc(iso):
            try:
                d = datetime.fromisoformat(str(iso))
            except ValueError:
                return None
            return (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc)
        start = utc(e.get("time")) or datetime.now(timezone.utc) - timedelta(days=30)
        end = utc(e.get("sold_time"))
        end = end + timedelta(hours=1) if end else datetime.now(timezone.utc)
        return start - timedelta(minutes=1), min(end, datetime.now(timezone.utc))

    def pl_estimate(self, e):
        """The trade's P/L from Schwab's latest list of its account's orders (their fills' prices) and the fee rule
        (otc_trade.estimate_fees), or None when the list has none of its fills."""
        start, end = self.trade_window(e)
        fee = FEE_USD if e.get("fee") is None else e["fee"]
        fills = []
        for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), []):
            if o.get("ticker") != e.get("ticker") or not o.get("filled"):
                continue
            try:
                when = datetime.strptime(o.get("entered") or "", "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                continue
            if not start <= when <= end:
                continue
            price = o.get("fill_price") or o.get("price") or 0
            fills.append({"order": o["id"], "side": o["side"], "time": o.get("filled_at") or o.get("entered"),
                          "shares": int(o["filled"]), "price": price, "value": round(o["filled"] * price, 4)})
        if not fills:
            return None
        return pl_summary(estimate_fees(fills, fee), "estimate")

    def trade_pl(self, e):
        """The trade's P/L: Schwab's (kept with it once read), else the estimate, else None."""
        if not e:
            return None
        return e.get("pl") or self.pl_estimate(e)

    def pl_tick(self):
        """(the main server's app) The sold trades whose Schwab transactions aren't read yet: asked for."""
        if self.trader is None or not self.decides():
            return
        now = time.time()
        for e in self.entries:
            if e.get("state") != "sold" or (e.get("pl") or {}).get("source") == "schwab":
                continue
            ask = e.get("pl_ask") or {}
            if ask.get("tries", 0) >= PL_TRIES or now - ask.get("at", 0) < PL_ASK_S:
                continue
            e["pl_ask"] = {"tries": ask.get("tries", 0) + 1, "at": now}
            start, end = self.trade_window(e)
            self.trader.read_transactions(e["id"], e.get("account") or self.chosen_account(), e["ticker"], start, end)

    def _transactions_read(self, key, fills, error):
        if str(key).startswith("actuals:"):          # the orders' Actual$ (every ticker of the account)
            if fills is not None:
                self.keep_actuals(key[len("actuals:"):], fills)
            return
        e = self.find(key)
        if e is None or fills is None:
            return
        start, end = self.trade_window(e)

        def when(f):
            try:
                return datetime.strptime(f["time"][:19] + "+0000", "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                return None
        mine = [f for f in fills if when(f) is not None and start <= when(f) <= end]
        pl = pl_summary(mine, "schwab")
        if pl["bought"] >= int(e.get("filled") or 0) and pl["sold"] >= int(e.get("sold") or 0) and pl["sold"]:
            e["pl"] = pl                # all of its fills: Schwab's P/L, kept with the trade (every computer)
            e.pop("pl_ask", None)
            e["note"] = f"P/L from Schwab: {usd_signed(pl['net'])} (fees ${pl['fees']:.2f})."
            self.save()
            self.changed.emit()

    def order_fills(self, e, order_id):
        """How many times the trade's order `order_id` was filled (Schwab's latest list), 0 when not known."""
        o = next((o for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), [])
                  if o["id"] == str(order_id or "")), None)
        return int((o or {}).get("fills") or 0)

    def fills_count(self, e, side):
        """How many times the trade's shares were bought / sold so far (its earlier orders' too), or None
        when Schwab's list doesn't tell (a server of an older version)."""
        field, prev = ("order_id", "fills_prev") if side == "BUY" else ("sell_id", "sell_fills_prev")
        o = next((o for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), [])
                  if o["id"] == str(e.get(field) or "")), None)
        if o is None or "fills" not in o:
            return None
        return int(o.get("fills") or 0) + int(e.get(prev) or 0)

    def commissions_count(self, e, side):
        """How many commissions the trade's buys / sells took so far: each of its orders once a market day it was
        filled on (an order filled in parts on the same day: one), or None when not known."""
        orders = [o for o in self.trade_orders(e) if o.get("side") == side and int(o.get("filled") or 0)]
        if not orders:
            return None
        # (a server of an older version: no fill_days, each order filled counts once)
        return sum(len(o.get("fill_days") or []) or 1 for o in orders)

    def fills_today_avg(self, e, side):
        """The trade's buys / sells in Schwab's latest list of orders: (its orders filled today - the market's day -,
        their fills' average price, True when filled at different prices), or None when the list has none."""
        orders = [o for o in self.trade_orders(e)
                  if o.get("side") == side and int(o.get("filled") or 0) and o.get("fill_price")]
        if not orders:
            return None
        today = market_day(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
        n_today = sum(1 for o in orders if today in (o.get("fill_days") or []))
        shares = sum(int(o["filled"]) for o in orders)
        avg = sum(int(o["filled"]) * float(o["fill_price"]) for o in orders) / shares
        # (an order filled in several fills: only their average is known; several orders' prices told apart)
        several = len({round(float(o["fill_price"]), 6) for o in orders}) > 1 or any(int(o.get("fills") or 0) > 1
                                                                                   for o in orders)
        return n_today, avg, several

    def live_order(self, e):
        """The trade's open order (its buy while buy_open, its sell while sell_open) in Schwab's latest list of
        its account's orders (order_info()), or None."""
        field = {"buy_open": "order_id", "sell_open": "sell_id"}.get(e.get("state"))
        oid = str(e.get(field) or "") if field else ""
        if not oid:
            return None
        return next((o for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), [])
                     if o["id"] == oid), None)

    def orders_read(self, account=None):
        """True once the account's (the chosen one's) orders were read from Schwab."""
        return (account or self.chosen_account()) in self.schwab_orders

    def chart_orders(self, ticker):
        """The ticker's open orders in the account chosen on the Monitor tab (one account at a time):
        its ATM trade's, then the account's others.
        [{"key", "side", "shares", "price", "atm", "duration", "account" (its last 3 digits)}]."""
        out = []
        e = self.active(ticker)
        chosen = self.chosen_account()
        if e and (e.get("account") or chosen) != chosen:
            e = None            # an ATM trade of another account: not shown
        if e and e.get("state") == "buy_open" and not e.get("gone"):
            out.append({"key": f"atm:{e['id']}:BUY", "side": "BUY", "atm": True, "duration": "GTC",
                        "shares": e["shares"] - (e.get("filled") or 0), "price": e["entry"],
                        "account": e.get("account") or chosen})
        elif e and e.get("state") == "sell_open" and e.get("sell_price") and not e.get("gone"):
            out.append({"key": f"atm:{e['id']}:SELL", "side": "SELL", "atm": True, "duration": "GTC",
                        "shares": self.remaining(e) - (e.get("sell_filled") or 0), "price": e["sell_price"],
                        "account": e.get("account") or chosen})
        for o in self.other_orders(ticker):
            duration = {"GOOD_TILL_CANCEL": "GTC", "DAY": "Day"}.get(o.get("duration"), o.get("duration"))
            out.append({"key": f"schwab:{o['id']}", "side": o["side"], "atm": False, "duration": duration,
                        "shares": o["shares"], "price": o["price"], "account": chosen})
        return out

    def _order_of_key(self, key):
        """(entry or None, side, the order: order_info() or None) of a chart order's key."""
        kind, _, rest = key.partition(":")
        if kind == "atm":
            eid, _, side = rest.rpartition(":")
            return self.find(eid), side, None
        account = self.chosen_account()
        o = next((o for o in self.schwab_orders.get(account, []) if o["id"] == rest), None)
        return None, (o or {}).get("side", ""), o

    def ask_cancel_key(self, parent, key):
        """The x on a chart order's tag: cancels it, after asking."""
        e, side, o = self._order_of_key(key)
        if e is not None:
            self.ask_cancel(parent, e, side)
            return
        if o is None or self.trader is None or key in self.canceling:
            return
        text = (f"Cancel the {side.lower()} order of {o['shares']:,} {o['ticker']} at {fmt_price(o['price'])}?\n"
                f"(A Schwab order, not an ATM trade.)")
        answer = QMessageBox.question(parent, f"Cancel {side.title()}", text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.canceling.add(key)
        self.cancel_ids[key] = o["id"]
        self.app_canceled.add(o["id"])
        self.changed.emit()
        self.trader.cancel(key, side, o["id"], self.chosen_account())

    def ask_move(self, parent, key, price):
        """A chart order's tag was dragged to `price`: after the dialog, Schwab replaces the order
        with one of the same amount of shares at that price."""
        e, side, o = self._order_of_key(key)
        if self.trader is None or not price or price <= 0:
            return
        if e is not None:
            if self.busy(e):
                return
            if side == "BUY" and e.get("state") == "buy_open":
                order_id, old, shares, done = e.get("order_id"), e["entry"], e["shares"], e.get("filled") or 0
                aon = bool(e.get("aon"))
            elif side == "SELL" and e.get("state") == "sell_open":
                order_id, old, shares, done = (e.get("sell_id"), e.get("sell_price"), self.remaining(e),
                                               e.get("sell_filled") or 0)
                aon = bool(e.get("sell_aon"))
            else:
                return
            if done and side == "BUY":      # partly bought: the rest canceled, then a new buy at the new price
                self.ask_rebuy(parent, e, shares, price, round(shares * price, 2))
                return
            if done:                        # partly sold: the rest canceled, then a new sell of all still owned
                self.ask_resell(parent, e, price)
                return
            ticker, account, fee, duration, session = e["ticker"], e.get("account"), e.get("fee"), None, None
            what = "an ATM trade"
        elif o is not None:
            if key in self.canceling or key in self.sending:
                return
            order_id, old, shares, ticker = o["id"], o["price"], o["shares"], o["ticker"]
            aon = bool(o.get("aon"))
            account, fee, duration, session = self.chosen_account(), None, o.get("duration"), o.get("session")
            if o.get("filled"):
                QMessageBox.warning(parent, "Change the order", f"{o['filled']:,} were already filled: "
                                    "change it in thinkorswim.")
                return
            what = "a Schwab order, not an ATM trade"
        else:
            return
        if not order_id:
            QMessageBox.warning(parent, "Change the order", "The order's number isn't known. Change it in Schwab.")
            return
        # the commission: Schwab's for the ticker as read now, else the trade's, else the usual one
        read = self.fee_of(ticker)
        fee = read if read is not None else fee
        estimated = fee is None
        fee = FEE_USD if estimated else fee
        aon_check = aon_box()               # as the order is now: can be changed with the price
        aon_check.setChecked(aon)

        def make(p):
            """The dialog's title and rows for the new price p (editable in it)."""
            total = shares * p
            rows = [("Ticker", ticker), ("Order", f"{side} · Limit · {what}"),
                    ("Amount of stocks", f"{shares:,}"),
                    ("Limit price now", fmt_price(old)), ("New limit price", PRICE_ROW), ("Fill", aon_check)
                    ] + fee_rows(None if estimated else fee, total, sell=side == "SELL")
            return f"Change the {side.lower()} of {shares:,} {ticker} from {fmt_price(old)} to {fmt_price(p)}?", rows
        if self.fee_reader:
            self.fee_reader.load_accounts()
        price = confirm_price_order(parent, self.account_name(account), price, make, f"Change {side} order",
                                    BUY_COLOR if side == "BUY" else SELL_COLOR)
        if price is None:
            return
        aon = aon_check.isChecked()
        busy_key = e["id"] if e is not None else key
        self.sending.add(busy_key)
        self.pending[busy_key] = {"old_id": str(order_id), "key": key, "aon": aon}
        self.changed.emit()
        self.trader.replace(busy_key, ticker, side, order_id, shares, price, account, duration, session, aon)

    def ask_buy_more(self, parent, e, start, default_shares, fee=None):
        """Buy More (the trade bought): after the dialog, a new buy for the same trade; once out the trade waits
        for the buy again (step 1), counting what it owns; bought: the average entry (_rebuy_placed, _done)."""
        if self.trader is None or self.busy(e) or e.get("state") != "bought" or e.get("rebuy") \
                or e.get("partial_sell"):
            return False
        if self.fee_reader:
            self.fee_reader.load_accounts()
        got = buy_more_dialog(parent, self.account_name(e.get("account")), e, start, default_shares,
                              e.get("fee") if fee is None else fee)
        if got is None or e.get("state") != "bought" or self.busy(e):
            return False
        shares, price, aon = got
        owned = self.remaining(e)
        total = int(e.get("filled") or 0) + shares
        self.sending.add(e["id"])
        self.pending[e["id"]] = {"rebuy": True, "shares": total, "price": price, "aon": aon,
                                 "size": round((owned + shares) * price, 2), "old_id": str(e.get("order_id") or ""),
                                 "more": shares}
        e["note"] = f"Buy More: sending a buy of {shares:,} @ {fmt_price(price)}..."
        self.changed.emit()
        self.trader.place(e["id"], e["ticker"], "BUY", shares, price, e.get("account"), aon)
        return True

    def ask_resell(self, parent, e, price):
        """An open ATM sell partly sold, changed (Update Sell, a new limit in All Orders or on the chart): after the
        dialog its rest is canceled; once Schwab says so (the trade "bought" again), a new sell goes out for all
        the stocks still owned at `price` (partial_followup). True when it was asked for."""
        if self.trader is None or self.busy(e) or e.get("state") != "sell_open" or e.get("resell"):
            return False
        sold_now = int(e.get("sell_filled") or 0)
        left = self.remaining(e) - sold_now
        if left <= 0:
            QMessageBox.information(parent, "Change the sell order", "All of them are sold already.")
            return False
        order_id = e.get("sell_id")
        if not order_id:
            QMessageBox.warning(parent, "Change the sell order", "The order's number isn't known. Change it in Schwab.")
            return False
        read = self.fee_of(e["ticker"])
        fee = read if read is not None else e.get("fee")
        aon = aon_box()
        aon.setChecked(bool(e.get("sell_aon")))
        sold = int(e.get("sold") or 0) + sold_now
        old = e.get("sell_price")
        rows = [("Ticker", e["ticker"]), ("Order", "SELL · Limit · GTC · an ATM trade, partly sold"),
                ("Sold so far", f"{sold:,} of {int(e.get('filled') or 0):,}" + (f" @ {fmt_price(old)}" if old else "")),
                ("The rest of the order", (f"canceled first, then a new sell of {left:,} @ {fmt_price(price)}",
                                           "#f0a020")),
                ("Limit price", (f"{fmt_price(old)} → {fmt_price(price)}", CHANGED_COLOR) if old else fmt_price(price)),
                ("Fill", aon)] + fee_rows(fee, left * price, sell=True)
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(e.get("account")), f"Sell the rest of {e['ticker']}: "
                             f"{left:,} @ {fmt_price(price)} (the old order's rest canceled first)?", rows,
                             "Cancel the rest + SELL new", SELL_COLOR):
            return False
        if self.busy(e) or e.get("state") != "sell_open":
            return False
        e["resell"] = {"price": price, "aon": aon.isChecked(), "by": socket.gethostname(), "at": time.time()}
        e["note"] = f"Update Sell: canceling the rest of the order, then selling {left:,} @ {fmt_price(price)}..."
        self.save()
        self.canceling.add(e["id"])
        self.cancel_ids[e["id"]] = str(order_id)
        self.app_canceled.add(str(order_id))
        self.changed.emit()
        self.trader.cancel(e["id"], "SELL", order_id, e.get("account"))
        return True

    def ask_rebuy(self, parent, e, shares, price, size):
        """An open ATM buy partly bought, changed (the checklist's new values, a new limit in All Orders or on
        the chart): after the dialog its rest is canceled; once Schwab says so, a new buy goes out for the shares
        still to buy (`shares` in all, less those bought) at `price` (partial_followup). The trade keeps counting
        what was bought before. True when it was asked for."""
        if self.trader is None or self.busy(e) or e.get("state") != "buy_open" or e.get("rebuy") \
                or e.get("partial_sell"):
            return False
        filled, shares = int(e.get("filled") or 0), int(shares)
        if shares <= filled:
            QMessageBox.warning(parent, "Change the buy order", f"{filled:,} are bought already: the amount in all "
                                "must be more than that (or sell them: ! Sell Partial).")
            return False
        order_id = e.get("order_id")
        if not order_id:
            QMessageBox.warning(parent, "Change the buy order", "The order's number isn't known. Change it in Schwab.")
            return False
        ticker, account = e["ticker"], e.get("account")
        read = self.fee_of(ticker)
        fee = read if read is not None else e.get("fee")
        estimated = fee is None
        fee = FEE_USD if estimated else fee
        rest = shares - filled

        def row(label, old, new):
            return (label, (f"{old} → {new}", CHANGED_COLOR)) if old != new else (label, new)
        aon = aon_box()
        aon.setChecked(bool(e.get("aon")))
        rows = [("Ticker", ticker), ("Order", "BUY · Limit · GTC · an ATM trade, partly bought"),
                ("Bought so far", f"{filled:,} of {e['shares']:,} @ {fmt_price(e['entry'])}"),
                ("The rest of the order", (f"canceled first, then a new buy of {rest:,} @ {fmt_price(price)}",
                                           "#f0a020")),
                row("Amount of stocks (in all)", f"{e['shares']:,}", f"{shares:,}"),
                row("Limit price", fmt_price(e["entry"]), fmt_price(price)), ("Fill", aon)
                ] + fee_rows(None if estimated else fee, rest * price)
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(account), f"Buy the rest of {ticker}: {rest:,} @ "
                             f"{fmt_price(price)} (the old order's rest canceled first)?", rows,
                             "Cancel the rest + BUY new", BUY_COLOR):
            return False
        if self.busy(e) or e.get("state") != "buy_open":
            return False
        e["rebuy"] = {"shares": shares, "price": price, "size": round(float(size), 2), "aon": aon.isChecked(),
                      "by": socket.gethostname(), "at": time.time()}
        e["note"] = f"Update Buy: canceling the rest of the order, then buying {rest:,} @ {fmt_price(price)}..."
        self.save()
        self.canceling.add(e["id"])
        self.cancel_ids[e["id"]] = str(order_id)
        self.app_canceled.add(str(order_id))
        self.changed.emit()
        self.trader.cancel(e["id"], "BUY", order_id, account)
        return True

    def ask_change_buy(self, parent, e, shares, price, size):
        """The checklist's boxes of an open buy (the position $, the entry, the amount of stocks) were
        changed: after the dialog, Schwab replaces the order with `shares` at `price`; atm.json gets
        the new values once it's done (_replaced)."""
        if self.trader is None or self.busy(e) or e.get("state") != "buy_open":
            return False
        if e.get("filled"):         # partly bought: the rest canceled, then a new buy for what's left to buy
            return self.ask_rebuy(parent, e, shares, price, size)
        order_id = e.get("order_id")
        if not order_id:
            QMessageBox.warning(parent, "Change the buy order", "The order's number isn't known. Change it in Schwab.")
            return False
        ticker, account = e["ticker"], e.get("account")
        read = self.fee_of(ticker)
        fee = read if read is not None else e.get("fee")
        estimated = fee is None
        fee = FEE_USD if estimated else fee
        old_shares, old_price = e["shares"], e["entry"]
        old_size = e.get("size") or e.get("usd") or old_shares * old_price
        total = shares * price

        def row(label, old, new):
            return (label, (f"{old} → {new}", CHANGED_COLOR)) if old != new else (label, new)
        rows = [("Ticker", ticker), ("Order", "BUY · Limit · GTC" + (" · All or none" if e.get("aon") else "")
                                    + " · an ATM trade"),
                row("Position", f"${old_size:,.2f}", f"${size:,.2f}"),
                row("Amount of stocks", f"{old_shares:,}", f"{shares:,}"),
                row("Limit price (entry)", fmt_price(old_price), fmt_price(price))
                ] + fee_rows(None if estimated else fee, total)
        title = (f"Change the buy of {ticker} from {old_shares:,} @ {fmt_price(old_price)} "
                 f"to {shares:,} @ {fmt_price(price)}?")
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(account), title, rows, "Change BUY order", BUY_COLOR):
            return False
        self.sending.add(e["id"])
        self.pending[e["id"]] = {"old_id": str(order_id), "key": f"atm:{e['id']}:BUY", "shares": int(shares),
                                 "size": round(float(size), 2)}
        self.changed.emit()
        self.trader.replace(e["id"], ticker, "BUY", order_id, int(shares), price, account, aon=bool(e.get("aon")))
        return True

    def _replaced(self, key, side, new_id, price, error):
        self.sending.discard(key)
        pending = self.pending.pop(key, {})
        if error:
            QMessageBox.warning(QApplication.activeWindow(), f"{side.title()} order not changed", error)
            self.changed.emit()
            return
        e = self.find(key)
        if e is not None and "shares" in pending:       # the checklist's change: the amount too
            old_shares, old_price = e["shares"], e["entry"]
            if pending["shares"] != old_shares:
                e.setdefault("shares-original", old_shares)
            e["shares"] = pending["shares"]
            self._adopt(e, side, {"id": str(new_id) if new_id else None, "price": price},
                        pending.get("old_id"), 0, "Changed in the checklist")
            e["size"] = pending["size"]         # (the $ as typed there, or the amount x the price)
            e["note"] = (f"Changed in the checklist: {old_shares:,} @ {fmt_price(old_price)} -> "
                         f"{e['shares']:,} @ {fmt_price(price)}.")
            self.save()
        elif e is not None:
            self._adopt(e, side, {"id": str(new_id) if new_id else None, "price": price},
                        pending.get("old_id"), 0, "Changed from the chart")
        if e is not None and "aon" in pending:     # All or none as chosen in the change dialog
            e["aon" if side == "BUY" else "sell_aon"] = bool(pending["aon"])
            self.save()
        self.check()            # the account's orders again: the new one, and its number if not known
        self.changed.emit()

    def _adopt(self, e, side, new, old_id, old_filled, how):
        """The ATM trade follows its order's replacement `new` (order_info(), or {"id", "price"}):
        the new order's number and price, kept in atm.json (the checklist and the Monitor tab show
        it, the chart draws it). old_filled: what the old order filled (a sell's)."""
        price = new["price"]
        if old_id:
            e.setdefault("replaced_ids", []).append(str(old_id))
        e.pop("gone", None)
        e.pop("status", None)
        if side == "BUY":
            e.setdefault("entry-original", e["entry"])
            old = e["entry"]
            # the plan follows the order: the same amount at the new price (the $ follows it)
            e.update(entry=price, usd=round(e["shares"] * price, 2), size=round(e["shares"] * price, 2),
                     note=f"{how}: the buy price {fmt_price(old)} -> {fmt_price(price)}.")
            if new.get("id"):
                e["order_id"] = new["id"]
        else:
            if old_filled:
                e["sold"] = (e.get("sold") or 0) + old_filled
                e["proceeds"] = (e.get("proceeds") or 0.0) + old_filled * (e.get("sell_price") or 0)
            old = e.get("sell_price")
            e.update(sell_price=price, sell_filled=0, note=f"{how}: the sell price "
                     f"{fmt_price(old) if old else NA} -> {fmt_price(price)}.")
            if new.get("id"):
                e["sell_id"] = new["id"]
            e["sell_at"] = price
            if e.get("exit") != price:      # the plan's exit: the sell order's price now
                e.setdefault("exit-original", e.get("exit"))
                e["exit"] = price
            if self.active(e["ticker"]) is e:     # the checklist's exit follows
                self.settings.setdefault("checklist_prices", {}).setdefault(e["ticker"], {})["exit"] = limit_text(price)
                self.save_settings(self.settings)
        self.save()
        self.update_live(e["ticker"])
        if side == "BUY":       # the plan's safety net: the live one for the new amount and price
            net = e.get("safety-net-live")
            if net is not None and net != e.get("safety-net"):
                e.setdefault("safety-net-original", e.get("safety-net"))
                e["safety-net"] = net
                self.save()

    def _replacement(self, e, side, old_id, shares):
        """The order that took the place of the trade's old order in thinkorswim: open, the same
        ticker, side and amount of shares, newer, not followed by another trade. None if none."""
        tracked = self.tracked_ids()
        found = [o for o in self.schwab_orders.get(e.get("account") or self.chosen_account(), [])
                 if o["open"] and o["ticker"] == e["ticker"] and o["side"] == side and o["price"]
                 and o["quantity"] == shares and o["id"] != str(old_id) and o["id"] not in tracked]
        return max(found, key=lambda o: o["entered"]) if found else None

    def _orders(self, account, orders, error):
        if orders is None:
            return                  # tried again at the next check
        before = self.schwab_orders.get(account)
        self.schwab_orders[account] = orders
        if before is not None:      # (not the first read: its orders are old news)
            for ev in self.order_events(account, before, orders):
                self.order_news.emit(ev)
                if ev["kind"] == "placed" and self.decides():
                    # (placed outside the app: its market when first seen; the app's own have theirs already)
                    QTimer.singleShot(3000, lambda a=account, i=ev["id"], t=ev["ticker"]:
                                      self.keep_order_quote(a, i, self.snapshot_now(t), seen=True))
        self.orders_time[account] = time.time()
        # the trades' own orders: their status from the list (as a check of each would say)
        by_id = {o["id"]: o for o in orders}
        for e in list(self.entries):
            if (e.get("account") or self.chosen_account()) != account:
                continue
            for state, side, field in (("buy_open", "BUY", "order_id"), ("sell_open", "SELL", "sell_id")):
                o = by_id.get(str(e.get(field)))
                if e.get("state") == state and o is not None:
                    self._checked(e["id"], side, {"orderId": o["id"], "status": o["status"],
                                                  "filledQuantity": o["filled"]}, "")
        if account == self.chosen_account() and self.trader is not None:
            # new tickers on the Monitor tab's list (another account chosen, say): their bid / ask now
            new = sorted({o["ticker"] for o in self.other_orders()} - set(self.market))
            if new:
                self.trader.read_quotes(new)
        if self.decides():
            self.adopt_outside_orders(account, orders)
            self.want_actuals(account, orders)      # the filled orders' Actual$ (Schwab's transactions)
        # trades whose order was canceled or replaced outside the app: follow the new one, or, once
        # the orders read after it show none, take it as canceled
        for e in list(self.entries) if self.decides() else []:
            gone = e.get("gone")
            if not gone or (e.get("account") or self.chosen_account()) != account:
                continue
            gone["reads"] = gone.get("reads", 0) + 1
            new = self._replacement(e, gone["side"], gone["old_id"], gone["shares"])
            if new is not None:
                self._adopt(e, gone["side"], new, gone["old_id"], gone.get("filled", 0), "Changed in thinkorswim")
            elif gone["reads"] >= 2:        # the second read since is surely after the change
                e.pop("gone", None)
                self._done(e, gone["side"], gone["status"], gone.get("filled", 0))
                self.save()
        self.changed.emit()

    def set_sell_at(self, e, price):
        """The price Sell ATM sells at (starts as the planned exit): the trade's exit now, the same on the
        checklist, the Monitor tab and Telegram (exit-live too). On the checklist (shared): pinned, or not
        pinned when it's the plan's exit again (unpinned, or the Monitor tab's reset)."""
        if not price or price <= 0:
            return
        changed = not (e.get("sell_at") == price and e.get("exit-live") == price)
        e["sell_at"] = e["exit-live"] = price
        if changed:
            self.save()
        if self.active(e["ticker"]) is e:
            prices = self.settings.setdefault("checklist_prices", {})
            mine = prices.setdefault(e["ticker"], {})
            pin = None if e.get("exit") and abs(price - e["exit"]) < 1e-9 else limit_text(price)
            if mine.get("exit") != pin:
                if pin is None:
                    mine.pop("exit", None)
                else:
                    mine["exit"] = pin
                if not mine:
                    prices.pop(e["ticker"], None)
                self.save_settings(self.settings)
                changed = True
            elif not mine:
                prices.pop(e["ticker"], None)
        if changed:
            self.update_live(e["ticker"])
            self.changed.emit()

    def reset(self, e):
        """The checklist leaves the fulfilled trade (it stays in atm.json) and its typed values."""
        e["reset"] = True
        self.save()
        self.settings.get("checklist_prices", {}).pop(e["ticker"], None)
        self.settings.get("checklist_ticks", {}).pop(e["ticker"], None)
        self.save_settings(self.settings)
        self.changed.emit()

    def clear(self, e):
        """Takes a sold or canceled trade out of atm.json."""
        if e.get("state") not in ("sold", "canceled"):
            return
        self.entries = [x for x in self.entries if x["id"] != e["id"]]
        self.save()
        self.changed.emit()

    def tick(self):
        """Every ORDER_CHECK_S: reads only what is due.
        Account activity stream off: all of it every NO_STREAM_CHECK_S.
        On: nothing, unless the Monitor tab is open (its quotes; the positions every POSITIONS_TAB_S)
        or an ATM trade has an order working (the orders every SAFETY_CHECK_S)."""
        if self.trader is None:
            return
        now = time.time()
        if not self.activity_live:
            if now - self.orders_checked >= NO_STREAM_CHECK_S - 1:
                self.check()
            return
        if self.tab_open:
            self._check_quotes()
            if now - self.positions_checked >= POSITIONS_TAB_S - 1 and self.chosen_account():
                self.positions_checked = now
                self.trader.read_positions(self.chosen_account())
        working = any(e.get("state") in ("buy_open", "sell_open") for e in self.entries)
        if working and now - self.orders_checked >= SAFETY_CHECK_S - 1:
            self.check(quotes=False)

    def order_event(self, kind, order_id):
        """An event of the account activity stream (OrderCreated, ChangeAccepted, a fill...): the
        orders and positions checked in a moment (one check for a few events together). With the
        server reading them, it does it by itself on the event, for all my computers."""
        if self.trader is not None and self.trader.link is not None and self.trader.link.is_connected:
            return
        self.event_timer.start()

    def set_activity_live(self, live):
        """The account activity stream on / off. On (connected, or again after a drop): the orders
        read once, for the events missed while it was off."""
        was, self.activity_live = self.activity_live, bool(live)
        if self.activity_live and not was and not (self.trader is not None and self.trader.link is not None):
            self.event_timer.start()        # (the server reads them by itself when its stream connects)

    def set_tab_open(self, shown):
        """The Monitor tab shown: all of it read at once, then its quotes and positions while shown."""
        was, self.tab_open = self.tab_open, bool(shown)
        if self.tab_open and not was:
            self.check()

    def check(self, orders=True, quotes=True):
        """Asks Schwab about every open order and the positions, and for the quotes (see
        _check_quotes). orders / quotes: False leaves those out."""
        if self.trader is None:
            return
        if quotes:
            self._check_quotes()
        if not orders:
            return
        self.orders_checked = self.positions_checked = time.time()
        for e in self.entries:
            if e["id"] in self.sending:
                continue
            if e.get("state") == "buy_open" and e.get("order_id"):
                self.trader.check(e["id"], "BUY", e["order_id"], e.get("account"))
            elif e.get("state") == "sell_open" and e.get("sell_id"):
                self.trader.check(e["id"], "SELL", e["sell_id"], e.get("account"))
        # the accounts' orders: the chosen one's are drawn on the chart; an open trade's show a
        # change made in thinkorswim
        # every account's: their tickers are streamed and recorded (tracked_tickers)
        accounts = {self.chosen_account()} | set(self.all_accounts()) | {
            e.get("account") for e in self.entries if e.get("state") in ("buy_open", "sell_open")}
        for account in sorted(a for a in accounts if a):
            self.trader.read_orders(account)
            self.trader.read_positions(account)     # the stocks owned (the chosen one's: the Monitor tab's list)

    def _check_quotes(self):
        # the open trades' tickers, and the chosen account's other orders' and stocks owned (the
        # Monitor tab's bid / ask); while the stream is on, they all come from it: none read
        tickers = [] if self.activity_live else self.stream_tickers()
        if tickers:
            self.quotes_checked = time.time()
            self.trader.read_quotes(tickers)

    # ---- answers from Schwab ----
    def _placed(self, key, side, order_id, message):
        self.sending.discard(key)
        pending = self.pending.pop(key, {})
        if pending.get("rebuy"):
            self._rebuy_placed(key, order_id, message, pending)
            return
        if order_id is None and not message.startswith("Schwab took"):
            QMessageBox.warning(QApplication.activeWindow(), f"{side} order not sent",
                                f"{pending.get('ticker') or (self.find(key) or {}).get('ticker', '')}: {message}")
            self.changed.emit()
            return
        now = datetime.now().isoformat(timespec="seconds")
        if side == "BUY":
            ticker = key
            e = {"id": str(order_id or f"{ticker}-{now}"), "order_id": order_id, **pending,
                 "exit-live": pending.get("exit"), "safety-net-live": pending.get("safety-net"),
                 "state": "buy_open", "filled": 0, "sold": 0, "sell_at": pending.get("exit"), "note": message}
            self.entries.append(e)
            self.buy_sent.emit(ticker)
        else:
            e = self.find(key)
            if e is None:
                return
            if e.get("sell_id") and e.get("sold"):        # an earlier sell's fills (Update Sell): counted on
                e["sell_fills_prev"] = int(e.get("sell_fills_prev") or 0) + self.order_fills(e, e["sell_id"])
            if e.get("sell_id") and str(e["sell_id"]) != str(order_id):     # (the old sell: still the trade's)
                e.setdefault("replaced_ids", []).append(str(e["sell_id"]))
            e.update(state="sell_open", sell_price=pending.get("sell_price"), sell_id=order_id,
                     sell_time=now, sell_filled=0, note=message, sell_aon=bool(pending.get("aon")),
                     sell_shares=pending.get("shares"))
            e.pop("status", None)
        self.save()
        self.changed.emit()
        if order_id:
            QTimer.singleShot(5000, self.check)

    def _rebuy_placed(self, key, order_id, message, pending):
        """Update Buy of a partly bought trade: its new buy for the rest went out (or not)."""
        e = self.find(key)
        if e is None:
            return
        e.pop("rebuy", None)
        bought = int(e.get("filled") or 0)
        if order_id is None and not message.startswith("Schwab took"):
            e.update(state="bought", bought_time=datetime.now().isoformat(timespec="seconds"),
                     note=f"{'Buy More' if pending.get('more') else 'Update Buy'}: the new buy wasn't sent ({message}). "
                          f"You own {self.remaining(e):,}.")
            e.pop("status", None)
            self.save()
            self.changed.emit()
            QMessageBox.warning(QApplication.activeWindow(), "BUY order not sent", f"{e['ticker']}: {message}")
            return
        if pending.get("old_id"):
            e.setdefault("replaced_ids", []).append(pending["old_id"])
            e["fills_prev"] = int(e.get("fills_prev") or 0) + self.order_fills(e, pending["old_id"])
        # what was bought before: counted with the new order's fills (filled), its cost for the average entry
        e["cost_prev"] = round((e.get("cost_prev") or 0.0) + (bought - (e.get("filled_prev") or 0)) * e["entry"], 6)
        e["filled_prev"] = bought
        e.setdefault("entry-original", e["entry"])
        if pending["shares"] != e["shares"]:
            e.setdefault("shares-original", e["shares"])
        price, shares = pending["price"], pending["shares"]
        e.pop("bought_time", None)
        e.update(state="buy_open", order_id=order_id, entry=price, shares=shares, usd=round(shares * price, 2),
                 size=pending.get("size") or round(shares * price, 2), aon=bool(pending.get("aon")),
                 note=(f"Buy More: {pending['more']:,} more @ {fmt_price(price)} ({bought:,} owned before)."
                       if pending.get("more") else
                       f"Update Buy: {bought:,} bought before; a new buy of {shares - bought:,} @ {fmt_price(price)}.")
                 + ("" if order_id else " " + message))
        e.pop("status", None)
        self.save()
        self.update_live(e["ticker"])
        net = e.get("safety-net-live")      # the plan's safety net: the live one for the new amount and price
        if net is not None and net != e.get("safety-net"):
            e.setdefault("safety-net-original", e.get("safety-net"))
            e["safety-net"] = net
            self.save()
        self.changed.emit()
        if order_id:
            QTimer.singleShot(5000, self.check)

    def _canceled(self, key, side, error):
        self.canceling.discard(key)
        order_id = self.cancel_ids.pop(key, None)
        if error:
            self.app_canceled.discard(order_id)
            e = self.find(key)
            if e is not None and e.pop("partial_sell", None):       # its sell isn't sent either
                e["note"] = "Sell Partial stopped: the rest of the buy wasn't canceled."
                self.save()
            if e is not None and e.pop("rebuy", None):              # nor the new buy
                e["note"] = "Update Buy stopped: the rest of the order wasn't canceled."
                self.save()
            if e is not None and e.pop("resell", None):             # nor the new sell
                e["note"] = "Update Sell stopped: the rest of the order wasn't canceled."
                self.save()
            QMessageBox.warning(QApplication.activeWindow(), f"{side.title()} order not canceled", error)
        else:
            e = self.find(key)
            if e is not None:
                e["note"] = f"Canceling the {side.lower()} order..."
            QTimer.singleShot(1500, self.check)     # Schwab says CANCELED a moment later
        self.changed.emit()

    def _checked(self, key, side, info, message):
        e = self.find(key)
        if info is None or e is None or key in self.sending:
            return                  # tried again at the next check; or the order is being changed
        if not self.decides():
            return                  # the main server's app does it
        if e.get("state") != ("buy_open" if side == "BUY" else "sell_open"):
            return                  # an older answer
        current = e.get("order_id" if side == "BUY" else "sell_id")
        old_id = str(info.get("orderId", current))
        if old_id != str(current):
            return                  # about an older order
        status = info.get("status", "")
        filled = int(info.get("filledQuantity") or 0)
        if e.get("gone"):
            return                  # waiting for the account's orders: changed in thinkorswim?
        # canceled or replaced, not by the app, before anything was bought (a sell: at any point):
        # maybe changed in thinkorswim. Followed if the account's orders show a new order of the
        # same kind and amount; else taken as canceled once they are read again.
        if status in OPEN_GONE and old_id not in self.app_canceled and (side == "SELL" or not filled):
            shares = e["shares"] if side == "BUY" else self.remaining(e) - filled
            new = self._replacement(e, side, old_id, shares)
            if new is not None:
                self._adopt(e, side, new, old_id, filled, "Changed in thinkorswim")
            else:
                e["gone"] = {"side": side, "old_id": old_id, "status": status, "filled": filled,
                             "shares": shares, "since": datetime.now().isoformat(timespec="seconds")}
                e["note"] = f"The {side.lower()} order was {status.lower()} in Schwab: looking for a new one..."
                self.save()
                self.trader.read_orders(e.get("account") or self.chosen_account())
            self.changed.emit()
            return
        self._done(e, side, status, filled)
        self.save()
        self.changed.emit()

    def _done(self, e, side, status, filled):
        """The trade's order's status from Schwab: what it did to the trade."""
        now = datetime.now().isoformat(timespec="seconds")
        e["status"] = status
        if side == "BUY":
            before = int(e.get("filled_prev") or 0)     # bought by the trade's orders before this one (Update Buy)
            e["filled"] = before + filled
            if e.get("rebuy") and status in ORDER_DONE and status != "FILLED":
                e["rebuy"]["ready"] = True      # the rest canceled: the new buy goes out (partial_followup)
                e["note"] = f"Update Buy: the rest canceled after {e['filled']:,}; sending the new buy..."
                return
            if status == "FILLED":
                e.pop("rebuy", None)      # (the rest bought before the cancel)
            if status == "FILLED" or (status in ORDER_DONE and e["filled"]):
                if before and e["filled"]:      # the average price of all of them
                    e.setdefault("entry-last", e["entry"])
                    e["entry"] = round(((e.get("cost_prev") or 0.0) + filled * e["entry"]) / e["filled"], 6)
                e.update(state="bought", bought_time=now,
                         note="" if status == "FILLED" else
                         f"The buy order was {status.lower()} after {e['filled']:,} of {e['shares']:,}.")
                e.pop("status", None)
            elif status in ORDER_DONE:      # nothing bought
                e.update(state="canceled", canceled_time=now,
                         note="" if status == "CANCELED" else f"The buy order was {status.lower()}.")
        else:
            e["sell_filled"] = filled
            if status in ORDER_DONE:
                e["sold"] = (e.get("sold") or 0) + filled
                e["proceeds"] = (e.get("proceeds") or 0.0) + filled * (e.get("sell_price") or 0)
                e.pop("sell_filled", None)
                e.pop("status", None)
                if self.remaining(e) <= 0:
                    e.update(state="sold", sold_time=now, note="")
                else:               # Sell ATM can send it again
                    e.update(state="bought", sell_end=status.lower(), note=f"The sell order was {status.lower()}"
                                                  + (f" after {filled:,} were sold." if filled else "."))

    # ---- the live exit and safety net, kept in atm.json beside the planned ones ----
    def update_live(self, ticker):
        """The checklist's live exit and safety net for the ticker's trade (typed on the checklist,
        else from the Ask and the Level II bids, as before the buy): "exit-live" and
        "safety-net-live" in atm.json, beside the planned "exit" and "safety-net"."""
        e = self.active(ticker)
        if e is None or e.get("state") not in ("buy_open", "bought", "sell_open"):
            return
        m = self.market.get(ticker, {})
        typed = self.settings.get("checklist_prices", {}).get(ticker, {})

        def number(key):
            try:
                v = float(typed.get(key, ""))
                return v if v > 0 else None
            except ValueError:
                return None
        shares = (self.remaining(e) if e.get("state") != "buy_open" else 0) or e.get("shares")
        fee = FEE_USD if e.get("fee") is None else e["fee"]
        p = plan((m.get("bid") or [None])[0], (m.get("ask") or [None])[0],
                 [(mm, price, size, 0) for mm, price, size in m.get("bids", [])],
                 None, e["entry"], number("exit"), number("stop"), fee, shares)
        # The exit: only one typed here (or the sell order's price) is written. The typed prices are
        # this computer's own (settings.json): another computer without them would put its automatic
        # exit from the market now (Ask - 1 tick) over the plan, and the Telegram "Sell as planned"
        # and the other apps' Sell at would follow it. Not typed: the trade's own exit stays.
        exit_live = p["exit"] if number("exit") is not None else None
        if e.get("state") == "sell_open" and e.get("sell_price"):
            exit_live = e["sell_price"]         # the sell order's price
        changed = False
        for key, value in (("exit-live", exit_live), ("safety-net-live", p["stop"])):
            if value is not None and e.get(key) != value:     # kept while not known (Level II off)
                e[key] = value
                changed = True
        if changed:
            self.save()
            self.changed.emit()         # the Monitor tab's New Price follows the live exit

    # ---- the market of the trades' tickers ----
    def _known(self, ticker):
        return any(e.get("ticker") == ticker for e in self.entries) or ticker in self.stream_tickers()

    def stream_tickers(self):
        """The tickers streamed (Level I and II) besides the chart's: the open ATM trades', and the
        chosen account's other orders' and stocks owned (the Monitor tab's), so their bid / ask are
        live instead of read on a timer."""
        return sorted(set(self.open_tickers()) | {o["ticker"] for o in self.other_orders()}
                      | {p["ticker"] for p in self.owned()})

    def on_quote(self, ticker, q):
        """A Level 1 quote from the stream (stream field names)."""
        if not self._known(ticker):
            return
        m = self.market.setdefault(ticker, {})
        for side in ("BID", "ASK"):
            if q.get(f"{side}_PRICE"):
                m[side.lower()] = [q[f"{side}_PRICE"], q.get(f"{side}_SIZE")]
        self.update_live(ticker)
        self.market_changed.emit(ticker)

    def on_book(self, ticker, bids, asks):
        if not self._known(ticker):
            return
        m = self.market.setdefault(ticker, {})
        m["bids"] = [[mm, price, size] for mm, price, size, *_ in bids]
        m["asks"] = [[mm, price, size] for mm, price, size, *_ in asks]
        self.update_live(ticker)
        self.market_changed.emit(ticker)

    def _quotes(self, quotes):
        """Quotes read with the order check: the bid and ask even while Level II is off."""
        for ticker, q in quotes.items():
            m = self.market.setdefault(ticker, {})
            for side in ("bid", "ask"):
                if q.get(f"{side}Price"):
                    m[side] = [q[f"{side}Price"], q.get(f"{side}Size")]
            self.update_live(ticker)
            self.market_changed.emit(ticker)


# ---------------------------------------------------------------- the panel ---
class ChecklistPanel(QWidget):
    ticks_changed = pyqtSignal()
    _fills_done = pyqtSignal(object, object)    # (from the reading thread) the fill chances' inputs, (rows, summary)
    fee_changed = pyqtSignal(str)       # a ticker's fee read from Schwab (the chart's yellow bar when high)

    def __init__(self, path: Path, settings: dict, save_settings, token_path: Path = None, parent=None, share=None):
        """share: the app's checklist panel, for a hidden copy of it (the Mini App's checklist of any ticker): the
        same ATM trades, Schwab trader and fees (nothing of its own reads or decides), its hooks left as they are."""
        super().__init__(parent)
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.shared_with = share
        if share is not None:
            self.fees, self.fee_reader, self.trader, self.atm = share.fees, share.fee_reader, share.trader, share.atm
            self.default_usd = lambda: share.default_usd()
            self.risk_of = lambda t: share.risk_of(t)
            share.fee_changed.connect(self.shared_fee_read)     # (a method: let go when this copy is deleted)
        else:
            self.fees = {}          # ticker -> (fee or None, details), read from Schwab once a session
            self.fee_reader = FeeReader(settings, token_path) if token_path else None
            self.trader = SchwabTrader(self.fee_reader) if self.fee_reader else None
            # The ATM trades (atm.json), shared with the Monitor tab and the chart
            self.atm = AtmTrades(path.parent / "atm.json", settings, save_settings, self.fee_reader, self.trader)
            self.atm.fee_of = lambda ticker: self.fees.get(ticker, (None, ""))[0]
            self.atm.read_fee = self.read_fee
            self.atm.chart_quote = lambda ticker: ({"bid": (self.bid, self.bid_size), "ask": (self.ask, self.ask_size),
                                                    "bids": self.bids, "asks": self.asks}
                                                   if ticker and ticker == self.ticker else None)
        self.atm.changed.connect(self.refresh)
        self.price_tools = {}   # price key -> (its +/- buttons, its reset button)
        if self.fee_reader and share is None:
            self.fee_reader.done.connect(self.fee_read)
        self.setObjectName("checklist")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.ticker = ""
        self.rows, self.market_days, self.range, self.period = [], [], None, 50
        self.reloaders, self.reloaders_of = {}, ""     # the Level II panel's reloaders, of that ticker
        self.bid = self.ask = None
        self.bid_size = self.ask_size = None
        self.bids, self.asks, self.l2_live = [], [], False
        self.widgets = []       # (item, mark label, text label, value label / line edit or None)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("cl_title")
        head.addWidget(self.title)
        head.addStretch(1)
        # (no reload button: checklist.txt is read again at the app's start; self.reload() still does it)
        self.steps_view = StepsView()           # the ATM trade's 4 steps, at the panel's left edge (right to left)
        head.addWidget(self.steps_view, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)
        trade = QHBoxLayout()
        trade.setSpacing(6)
        self.buy_btn = QPushButton("Buy ATM")           # Cancel Buy while the buy waits
        self.buy_btn.setObjectName("cl_buy")
        self.buy_btn.clicked.connect(self.buy_clicked)
        self.sell_btn = QPushButton("Sell ATM")         # Cancel Sell while the sell waits
        self.sell_btn.setObjectName("cl_sell")
        self.sell_btn.clicked.connect(self.sell_clicked)
        self.done_btn = QPushButton("ATM fulfilled!\nPress to reset")     # on their left once sold
        self.done_btn.setObjectName("cl_done")
        self.done_btn.setToolTip("Sold. Resets the checklist of this ticker to its automatic values. "
                                 "The trade stays on the Monitor tab until you clear it there.")
        self.done_btn.clicked.connect(self.reset_done)
        self.done_btn.hide()
        self.bid_btn = QPushButton("Sell on Bid")      # beside Sell ATM while the stocks are owned
        self.bid_btn.setObjectName("cl_bid")
        self.bid_btn.setIcon(red_bang_icon())
        self.bid_btn.setToolTip("Sell all the stocks now: at the best Bid price that the Level II bids, added up "
                                "from the best down, fill all of them (after you approve it)")
        self.bid_btn.clicked.connect(self.sell_on_bid)
        self.bid_btn.hide()
        self.update_btn = QPushButton("Update Buy")    # beside Cancel Buy once new values are typed
        self.update_btn.setObjectName("cl_update")
        self.update_btn.clicked.connect(self.update_buy)
        self.update_btn.hide()
        self.update_sell_btn = QPushButton("Update Sell")   # beside Cancel Sell once a new exit is typed
        self.update_sell_btn.setObjectName("cl_update")
        self.update_sell_btn.clicked.connect(self.update_sell)
        self.update_sell_btn.hide()
        self.sell_draft = {}    # a new exit typed for the open sell order: {"id", "exit"}
        for b in (self.buy_btn, self.update_btn, self.sell_btn, self.update_sell_btn, self.bid_btn, self.done_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setLayoutDirection(Qt.LayoutDirection.LeftToRight)     # the X before the text
            trade.addWidget(b)
        trade.addStretch()
        root.addLayout(trade)
        self.order_label = QLabel()
        self.order_label.setObjectName("cl_order")
        self.order_label.setWordWrap(True)
        self.order_label.hide()
        root.addWidget(self.order_label)
        self.summary = QLabel()
        self.summary.setObjectName("cl_summary")
        root.addWidget(self.summary)
        # the fill chances (otc_fills): on the recorded days, would the buy at the entry, then the sell at the exit,
        # have filled - a line, the days on hover
        self.fills_label = QLabel()
        self.fills_label.setObjectName("cl_fills")
        self.fills_label.setWordWrap(True)
        self.fills_label.hide()
        root.addWidget(self.fills_label)
        self.fills_key, self.fills_at = None, 0.0
        self.fills_summary, self.fills_days = "", []        # (the Mini App's too)
        self._fills_done.connect(self.show_fills)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # No scroll bar (it took room on the left and was hard to see); the mouse wheel still scrolls
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)
        # ...so a small down arrow at the bottom left (over the text) tells there is more below; a click scrolls down
        self.more_below = QToolButton(self.scroll)
        self.more_below.setObjectName("cl_more")
        self.more_below.setText("▼")
        self.more_below.setFixedSize(22, 22)
        self.more_below.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_below.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_below.setToolTip("More below: scroll down (the mouse wheel, or click here)")
        self.more_below.setStyleSheet("QToolButton#cl_more { background: rgba(58, 64, 70, 220); color: #e8e8e8; "
                                      "border: 1px solid #5c6268; border-radius: 11px; font-size: 9px; padding: 0px; }"
                                      "QToolButton#cl_more:hover { background: rgba(90, 98, 106, 240); }")
        bar = self.scroll.verticalScrollBar()
        self.more_below.clicked.connect(lambda: bar.setValue(bar.value() + max(40, self.scroll.viewport().height() * 3 // 4)))
        bar.valueChanged.connect(self.show_more_below)
        bar.rangeChanged.connect(self.show_more_below)
        self.scroll.installEventFilter(self)
        self.more_below.hide()
        self.load()

    def show_more_below(self, *_):
        """The down arrow: shown while there is more to scroll down to, at the bottom left."""
        bar = self.scroll.verticalScrollBar()
        self.more_below.move(4, self.scroll.height() - self.more_below.height() - 4)
        self.more_below.setVisible(bar.value() < bar.maximum())
        self.more_below.raise_()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "scroll", None) and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.show_more_below()
        return super().eventFilter(obj, event)

    def reload(self):
        self.fees.pop(self.ticker, None)
        self.load()
        self.read_fee()
        self.atm.check()

    def load(self):
        try:
            title, sections = parse(self.path)
        except OSError as e:
            title, sections = "צ'ק ליסט", []
            self.summary.setText(f"Can't read {self.path.name}: {e.strerror}")
        self.title.setText(title)
        body = QWidget()
        body.setObjectName("cl_body")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)          # (condensed)
        self.widgets = []
        self.price_tools = {}
        self.locked = {}        # key -> its fixed cell (the plan sent with Buy ATM), shown once sent
        self.order_edits = {}   # key -> its box while the buy order is open (ORDER_EDITS): changes it
        self.changing_order = False     # the change-order dialog is open
        self.draft = {}         # new values typed for the open buy: {"id", "entry", "size", "shares", "last"}
        # the default position $ (the app sets it: the chosen account's, in the shared data); a hidden copy: the app's
        if getattr(self, "shared_with", None) is None:
            self.default_usd = lambda: default_position(self.settings)
        self.draft_tools = {}   # key -> the +/- and reset buttons of its new value's box (an open buy)
        self.boxes = {}         # key -> its live value's box
        self.row_boxes = {}     # key -> the row's value part: its boxes, +/-, pin (the Mini App works them)
        self.new_plan_btn = None    # by the ticker: an open buy's plan with the new values copied
        self.sections = sections        # (the Mini App reads the rows by them)
        for name, items in sections:
            head = QLabel(name)
            head.setObjectName("cl_section")
            lay.addWidget(head)
            for item in items:
                row = QHBoxLayout()
                row.setSpacing(6)
                mark = QLabel()
                mark.setFixedWidth(18)
                mark.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                text = QLabel(item.text)
                text.setWordWrap(True)
                value = None
                if item.kind == "manual":
                    mark.setCursor(Qt.CursorShape.PointingHandCursor)
                    mark.setToolTip("Click: empty → ✔ → ✘ → empty")
                    mark.mousePressEvent = lambda _e, it=item: self.toggle(it)
                    text.setCursor(Qt.CursorShape.PointingHandCursor)
                    text.mousePressEvent = lambda _e, it=item: self.toggle(it)
                if item.kind == "info" and item.key not in TARGETS and item.key not in ("shares", "ticker", "loss"):
                    mark.setFixedWidth(4)       # (the amount of stocks: its minimum's mark)
                if item.kind == "info" and item.key in EDITABLE:
                    value = self.make_edit(item.key)
                elif item.kind == "info" and item.key == "shares":
                    value = self.make_shares_edit()
                elif item.kind != "manual":
                    value = QLabel()
                    value.setObjectName("cl_value")
                    value.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                row.addWidget(mark)
                row.addWidget(text, 1)
                if value is not None:
                    shown = value.shown if isinstance(value, CopyLineEdit) else value.text
                    if item.key == "ticker":        # the ticker copies the whole trade plan
                        box = CopyBox(value, shown, copy_text=self.plan_text)
                        # beside the name: the ticker's risk sign (its last letter), as the chart's
                        self.ticker_risk_sign = QLabel()
                        self.ticker_risk_sign.hide()
                        box.layout().addWidget(self.ticker_risk_sign)
                        value.setCursor(Qt.CursorShape.PointingHandCursor)
                        value.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                        value.mousePressEvent = lambda e, b=box: b.copy() if e.button() == Qt.MouseButton.LeftButton else None
                        # an open buy (state 2): above the new values' column, the plan with them copied
                        self.boxes[item.key] = box
                        group = QWidget()
                        group.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                        glay = QHBoxLayout(group)
                        glay.setContentsMargins(0, 0, 0, 0)
                        glay.setSpacing(2)
                        box.setFixedWidth(LOCKED_W)         # (over the fixed cells' column)
                        glay.addWidget(box)
                        self.new_plan_btn = QToolButton()
                        self.new_plan_btn.setObjectName("cl_new_plan")
                        self.new_plan_btn.setIcon(copy_icon())
                        self.new_plan_btn.setText("New plan")
                        self.new_plan_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                        self.new_plan_btn.setToolTip("Copy the trade plan with the new values (this column)")
                        self.new_plan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                        self.new_plan_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                        self.new_plan_btn.clicked.connect(
                            lambda: copy_value(self.new_plan_text(), self.new_plan_btn, as_is=True))
                        self.new_plan_btn.hide()
                        glay.addWidget(self.new_plan_btn)
                        glay.addStretch()
                        box = group
                    else:
                        box = CopyBox(value, shown)
                        self.boxes[item.key] = box
                    if item.key in LOCKED or item.key in ORDER_EDITS:
                        # the fixed cell, the box, +/- and the pin close together, left to right
                        group = QWidget()
                        group.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                        glay = QHBoxLayout(group)
                        glay.setContentsMargins(0, 0, 0, 0)
                        glay.setSpacing(2)
                        if item.key in LOCKED:
                            lock = QLabel()
                            lock.setObjectName("cl_locked")
                            lock.setFixedWidth(LOCKED_W)       # the live cells beside them in one column
                            lock.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                            lock.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                            lock.hide()
                            lock.setFixedHeight(value.sizeHint().height() if isinstance(value, QLineEdit) else 22)
                            self.locked[item.key] = lock
                            glay.addWidget(lock, 0, Qt.AlignmentFlag.AlignVCenter)
                        if item.key in ORDER_EDITS:     # (in the fixed cell's place while the buy is open)
                            oe = self.make_order_edit(item.key)
                            self.order_edits[item.key] = oe
                            glay.addWidget(oe, 0, Qt.AlignmentFlag.AlignVCenter)
                        glay.addWidget(box)
                        if item.key in EDITABLE and item.key != "size":
                            stepper, pin = self.make_stepper(item.key, value), self.make_pin(item.key, value)
                            self.price_tools[item.key] = (stepper, pin)
                            glay.addWidget(stepper)
                            glay.addWidget(pin)
                        if item.key in ORDER_EDITS:     # an open buy's new values: +/- (the amounts) and a pin
                            tools = ([] if item.key == "entry" else [self.make_draft_stepper(item.key, value)]) \
                                + [self.make_draft_pin(item.key, value)]
                            for w in tools:
                                w.hide()
                                glay.addWidget(w)
                            self.draft_tools[item.key] = tools
                        if item.key not in LOCKED:
                            glay.addStretch()
                        box = group
                    row.addWidget(box)
                    self.row_boxes[item.key] = box
                # thin lines: | the ticker | the amounts | the prices | the % results | the fee | the $ results | section 1
                if item.key in SPLIT_BEFORE:
                    lay.addWidget(self.split_line())
                lay.addLayout(row)
                self.widgets.append((item, mark, text, value))
                if item.key in SPLIT_AFTER:
                    lay.addWidget(self.split_line())
        lay.addStretch()
        self.scroll.setWidget(body)
        self.refresh()

    def make_edit(self, key):
        edit = CopyLineEdit()
        edit.setFixedWidth(84)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(
            r"\$?\d{0,7}(\.\d{0,6})?" if key == "size" else r"\d{0,6}(\.\d{0,6})?"), edit))
        pal = edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#f0a020"))              # a typed-over value
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#ffffff"))    # the automatic value
        edit.setPalette(pal)
        edit.textEdited.connect(lambda text, k=key: self.typed(k, text))
        edit.editingFinished.connect(lambda k=key, e=edit: self.edit_done(k, e))
        return edit

    @staticmethod
    def split_line():
        """A thin line between the checklist's groups of rows."""
        line = QWidget()
        line.setObjectName("cl_split")
        line.setFixedHeight(1)
        line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        line.setStyleSheet("QWidget#cl_split { background: #3a4046; }")
        return line

    def make_shares_edit(self):
        """The amount of stocks: the automatic one (position $ / entry) shown, or type one: Enter sets the
        position $ to it x the entry, so the amount follows from it as usual."""
        edit = CopyLineEdit()
        edit.setFixedWidth(84)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[\d,]{0,12}"), edit))
        pal = edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#f0a020"))              # being typed
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#ffffff"))    # the amount now
        edit.setPalette(pal)
        edit.textEdited.connect(lambda text: self.drafting() is not None and self.draft_typed("shares", text))
        edit.editingFinished.connect(lambda e=edit: self.shares_done(e))
        return edit

    def shares_done(self, edit):
        """An amount of stocks typed: pinned for this ticker (the position $ follows it: amount x entry)."""
        if self.drafting() is not None:         # a new amount for the open buy
            self.draft_done("shares", edit)
            return
        if edit.isReadOnly():
            return
        text = edit.text().replace(",", "").strip()
        try:
            shares = int(text)
        except ValueError:
            shares = 0
        if shares > 0 and self.ticker:
            self.typed("shares", str(shares))       # pinned for this ticker: the $ follows it
            self.save_settings(self.settings)
        self.refresh()

    def make_order_edit(self, key):
        """A box of the open buy order (the position $, the entry or the amount of stocks), in the
        fixed cells' blue: Enter (or leaving it) after a change asks to change the order."""
        edit = QLineEdit()
        edit.setObjectName("cl_order_edit")
        edit.setFixedWidth(LOCKED_W)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(
            {"size": r"\$?[\d,]{0,9}(\.\d{0,2})?", "shares": r"[\d,]{0,12}"}.get(key, r"\d{0,6}(\.\d{0,6})?")), edit))
        edit.setStyleSheet(f"color: {ORDER_COLOR};")
        edit.setReadOnly(True)
        edit.setToolTip({"size": "The buy order's position in $ now.", "entry": "The buy order's limit price now.",
                         "shares": "The buy order's amount of stocks now."}[key]
                        + " To change the order: type the new values beside it, then Update Buy at the top.")
        edit.hide()
        return edit

    def can_change_order(self, e):
        return (e is not None and e.get("state") == "buy_open" and not e.get("rebuy") and not e.get("partial_sell")
                and self.trader is not None and not self.atm.busy(e, self.ticker))

    @staticmethod
    def order_size(e):
        return e.get("size") or e.get("usd") or e["shares"] * e["entry"]

    def order_edit_text(self, key, e):
        if key == "size":
            return f"${self.order_size(e):,.2f}"
        if key == "entry":
            return fmt_price(e["entry"])
        return f"{e['shares']:,}"

    # ---- new values for the open buy (the right column), sent with Update Buy ----
    def drafting(self):
        """The open buy whose values can be changed now (new values go beside its own), or None."""
        e = self.order()
        return e if self.can_change_order(e) else None

    def draft_of(self, e):
        if self.draft.get("id") != e["id"]:
            self.draft = {"id": e["id"]}
        return self.draft

    @staticmethod
    def draft_number(text, price=False):
        """A typed value as a number (a price typed as 1 or more: 0.0001 steps), or None."""
        try:
            v = Decimal((text or "").replace(",", "").replace("$", "").strip())
        except InvalidOperation:
            return None
        if v <= 0:
            return None
        return float(v * PRICE_STEP if price and v >= 1 else v)

    def draft_values(self, e):
        """(price, shares, size, changed): the buy with the new values typed (a new price keeps the
        amount of stocks, the $ follows; the amount or the $ typed last sets the other)."""
        d = self.draft_of(e)
        price = self.draft_number(d.get("entry"), price=True) or e["entry"]
        typed_shares, typed_size = self.draft_number(d.get("shares")), self.draft_number(d.get("size"))
        source = d.get("last")
        if source == "shares" and not typed_shares or source == "size" and not typed_size:
            source = None
        source = source or ("shares" if typed_shares else "size" if typed_size else None)
        if source == "size":
            size = typed_size
            shares = round(size / price)
        elif source == "shares" or abs(price - e["entry"]) > 1e-9:
            shares = int(typed_shares) if source == "shares" else e["shares"]    # (a new price: the same amount)
            size = shares * price
        else:
            size, shares = self.order_size(e), e["shares"]
        changed = shares >= 1 and (shares != e["shares"] or abs(price - e["entry"]) > 1e-9)
        return price, shares, size, changed

    def draft_typed(self, key, text):
        e = self.drafting()
        if e is None:
            return
        d = self.draft_of(e)
        d[key] = text.strip()
        if key in ("size", "shares"):
            other = "shares" if key == "size" else "size"
            if d[key]:
                d[other] = ""       # one of the two is typed, the other follows it (shown in white)
            d["last"] = key if d[key] else None
        self.refresh()

    def draft_done(self, key, edit):
        """Enter or leaving a new value's box: shown as the others are (12 -> 0.0012, $150)."""
        text = edit.text().strip()
        if text and key == "entry":
            v = self.draft_number(text, price=True)
            text = price_text(format(Decimal(str(v)), "f")) if v else ""
        elif text and key == "size":
            text = text.lstrip("$").strip()
        if self.drafting() is not None:
            self.draft_typed(key, text)

    def make_draft_stepper(self, key, edit):
        """+ above - beside the new $ ($10) or amount (1,000 under $1, else 10) of an open buy."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        what = "$10" if key == "size" else "1,000 stocks (10 at $1 and up)"
        for sign, direction in (("+", 1), ("−", -1)):
            b = QToolButton()
            b.setObjectName("cl_step")
            b.setText(sign)
            b.setFixedSize(18, 9)          # (condensed rows)
            b.setToolTip(f"{'Up' if direction > 0 else 'Down'} {what}")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, d=direction: self.draft_step(key, edit, d))
            lay.addWidget(b)
        return box

    def make_draft_pin(self, key, edit):
        """The pin of an open buy's new value: yellow when one is typed (kept), white when it follows the
        order (or the other new values). A click pins the value shown, or unpins it."""
        b = QToolButton()
        b.setObjectName("cl_pin")
        b.setFixedSize(18, 19)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda: self.draft_pin(key, edit))
        self.show_pin(b, False)
        return b

    def plan_pinned(self, key):
        """Before a trade: the $ pinned (for all tickers, unless this ticker's amount is), or the amount
        pinned (for this ticker)."""
        amount = self.override_text("shares")
        return bool(amount) if key == "shares" else bool(self.override_text("size")) and not amount

    def plan_step(self, key, edit, direction):
        """+/- before a trade: the $ to the next $10 (pinned for all tickers), the amount to the next
        1,000 (10 at $1 and up; pinned for this ticker)."""
        if not getattr(self, "last_plan", None) or not self.ticker and key == "shares":
            return
        p, usd = self.last_plan
        if key == "size":
            new = max((usd // 10 + 1) * 10 if direction > 0 else (-(-usd // 10) - 1) * 10, 10)
            text = format(Decimal(str(new)).normalize(), "f")
            edit.setText(f"${text}")
        else:
            shares, price = p.get("shares") or 0, p.get("entry") or 0
            if not price:
                return
            step = 1000 if price < 1 else 10
            new = max((shares // step + 1) * step if direction > 0 else (shares - 1) // step * step, step)
            text = str(int(new))
        self.typed(key, text)
        self.save_settings(self.settings)

    def plan_pin(self, key, edit):
        """The pin before a trade: pinned, unpin (the $: the default again; the amount: it follows the $);
        else the value shown kept."""
        if self.plan_pinned(key):
            text = ""
        else:
            text = edit.shown().replace(",", "").replace("$", "").strip()
            try:
                if float(text) <= 0:
                    return
            except ValueError:
                return
            if key == "size":
                text = format(Decimal(text).normalize(), "f")
            elif not self.ticker:
                return
        if key == "shares" or text:
            self.typed(key, text)
        else:
            self.typed("size", "")
        if not text:
            edit.clear()
        self.save_settings(self.settings)
        self.refresh()

    def draft_step(self, key, edit, direction):
        e = self.drafting()
        if e is None:
            if self.order() is None:
                self.plan_step(key, edit, direction)
            return
        price, shares, size, _ = self.draft_values(e)
        if key == "size":
            new = max((size // 10 + 1) * 10 if direction > 0 else (-(-size // 10) - 1) * 10, 10)  # to the next $10
            text = format(Decimal(str(new)).normalize(), "f")
            edit.setText(f"${text}")
        else:
            step = 1000 if price < 1 else 10
            new = max((shares // step + 1) * step if direction > 0 else (shares - 1) // step * step, step)
            text = str(int(new))
            edit.setText(f"{int(new):,}")
        self.draft_typed(key, text)

    def draft_pin(self, key, edit):
        """Pinned: unpin (back to the order's value, or what follows from the other new values). Not
        pinned: the value shown now kept as a typed one."""
        e = self.drafting()
        if e is None:
            if self.order() is None:
                self.plan_pin(key, edit)
            return
        if self.draft_of(e).get(key):
            text = ""
        else:
            text = edit.shown().replace(",", "").replace("$", "").strip()
            if self.draft_number(text, price=key == "entry") is None:
                return
            if key == "size":
                text = format(Decimal(text).normalize(), "f")      # $180.00 -> 180
        self.draft_typed(key, text)
        shown = self.draft.get(key, "")
        edit.setText(f"${shown}" if key == "size" and shown else f"{int(shown):,}" if key == "shares" and shown
                     else price_text(shown) if shown else "")
        self.refresh()

    def update_buy(self):
        """Update Buy: the open buy changed to the new values, after the usual dialog."""
        e = self.drafting()
        if e is None or self.changing_order:
            return
        price, shares, size, changed = self.draft_values(e)
        if not changed:
            return
        self.changing_order = True
        try:
            sent = self.atm.ask_change_buy(self, e, shares, price, size)
        finally:
            self.changing_order = False
        if sent:
            self.draft = {}
            for key in ("size", "entry", "shares"):
                box = next((v for it, _, _, v in self.widgets if it.key == key), None)
                if isinstance(box, QLineEdit):
                    box.clear()
        self.refresh()

    def make_stepper(self, key, edit):
        """+ above - beside a price box: one 0.0001 step up or down (held: repeats)."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        for sign, step in (("+", 1), ("−", -1)):
            b = QToolButton()
            b.setObjectName("cl_step")
            b.setText(sign)
            b.setFixedSize(18, 9)          # (condensed rows)
            b.setToolTip(f"{'Up' if step > 0 else 'Down'} 0.0001")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, s=step: self.step(key, edit, s))
            lay.addWidget(b)
        return box

    def make_pin(self, key, edit):
        """The pin: yellow when the price is pinned (typed, stepped, or pinned as it was; saved), white
        when it's the automatic one. A click pins the price shown, or unpins it."""
        b = QToolButton()
        b.setObjectName("cl_pin")
        b.setFixedSize(18, 19)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda: self.toggle_pin(key, edit))
        self.show_pin(b, False)
        return b

    @staticmethod
    def show_pin(button, pinned):
        if button.property("pinned") == pinned:
            return
        button.setProperty("pinned", pinned)
        button.setIcon(pin_icon(pinned))
        button.setToolTip("Pinned: this price is kept (saved) and the automatic one isn't used.\n"
                          "Click to unpin: back to the automatic value." if pinned else
                          "Not pinned: the automatic value.\nClick to pin it as it is now (typing a price "
                          "or + / - pins it too).")

    def selling(self, key):
        """The ticker's trade when `key` is the exit of its open sell order (a new exit changes the order)."""
        e = self.order()
        return e if key == "exit" and e and e.get("state") == "sell_open" and e.get("sell_price") else None

    def sell_draft_price(self, e):
        """The new exit typed for the open sell order e (a number), or None."""
        if not e or self.sell_draft.get("id") != e["id"]:
            return None
        return self.draft_number(self.sell_draft.get("exit"), price=True)

    def set_sell_draft(self, e, text):
        """A new exit for the open sell: only shown, with what it gives (Update Sell sends it)."""
        self.sell_draft = {"id": e["id"], "exit": text.strip()} if text.strip() else {}
        self.refresh()

    def update_sell(self):
        """Update Sell: the open sell order changed to the new exit, after the usual dialog (as dragging
        its tag on the chart); sent, the new price is the pinned exit (on every computer)."""
        e = self.selling("exit")
        price = self.sell_draft_price(e)
        if e is None or not price or abs(price - (e.get("sell_price") or 0)) < 1e-9:
            return
        self.atm.ask_move(self, f"atm:{e['id']}:SELL", price)
        if e["id"] in self.atm.sending or e.get("resell"):     # sent (not canceled in the dialog)
            self.sell_draft = {}
            prices = self.settings.setdefault("checklist_prices", {})
            prices.setdefault(e["ticker"], {})["exit"] = limit_text(price)
            self.save_settings(self.settings)
            box = self.boxes and next((v for it, _, _, v in self.widgets if it.key == "exit"), None)
            if isinstance(box, QLineEdit):
                box.clear()
        self.refresh()

    def toggle_pin(self, key, edit):
        if not edit.isEnabled() or edit.isReadOnly():
            return
        e = self.selling(key)
        if e:           # the open sell: pinned = a new exit typed; unpin = back to the order's price
            if self.sell_draft_price(e) is not None:
                edit.clear()
                self.set_sell_draft(e, "")
            return
        if self.override_text(key):         # pinned: unpin, the automatic value again
            edit.clear()
            self.typed(key, "")
        else:                               # the value shown now, kept
            text = edit.shown().replace(",", "").replace("$", "").strip()
            try:
                if float(text) <= 0:
                    return
            except ValueError:
                return
            edit.setText(text)
            self.typed(key, text)
        self.save_settings(self.settings)

    def step(self, key, edit, direction):
        """Moves the price shown (typed, or else the automatic one) one 0.0001 step. The exit of an open
        sell order: asks to change the order to it."""
        if not edit.isEnabled():
            return
        e = self.selling(key)
        try:
            base = Decimal(str(self.sell_draft_price(e) or e["sell_price"])) if e else \
                Decimal(edit.shown().replace(",", "").replace("$", "").strip())
        except InvalidOperation:
            return
        new = base + direction * PRICE_STEP
        if new <= 0:
            return
        if e:                                   # a new exit for the open sell: shown, not sent
            edit.setText(format(new, "f"))
            self.set_sell_draft(e, format(new, "f"))
            return
        text = format(new, "f")
        edit.setText(text)
        self.typed(key, text)
        self.save_settings(self.settings)

    # ---- typed-over values: the size for all tickers, the prices per ticker ----
    def overrides(self):
        """{key: number} of the typed-over values in use for the current ticker."""
        out = {}
        raw = dict(self.settings.get("checklist_prices", {}).get(self.ticker, {}))
        if self.settings.get("checklist_size"):
            raw["size"] = self.settings["checklist_size"]
        for key, text in raw.items():
            try:
                number = float(text)
            except (TypeError, ValueError):
                continue
            if number > 0:
                out[key] = number
        return out

    def override_text(self, key):
        if key == "size":
            return self.settings.get("checklist_size", "")
        return self.settings.get("checklist_prices", {}).get(self.ticker, {}).get(key, "")

    def typed(self, key, text):
        e = self.selling(key)
        if e:
            self.set_sell_draft(e, text)        # the open sell's new exit: Update Sell sends it
            return
        if key in ("size", "entry") and self.drafting() is not None:
            self.draft_typed(key, text)         # a new value for the open buy, not the plan's
            return
        text = text.strip()
        if key == "size":
            text = text.lstrip("$").strip()      # (shown as $150)
        try:
            valid = float(text) > 0
        except ValueError:
            valid = False
        if key == "size":
            if valid:
                self.settings["checklist_size"] = text
                mine = self.settings.get("checklist_prices", {}).get(self.ticker)
                if mine and mine.pop("shares", None) is not None and not mine:
                    self.settings["checklist_prices"].pop(self.ticker, None)
            else:
                self.settings.pop("checklist_size", None)
        elif self.ticker:
            prices = self.settings.setdefault("checklist_prices", {})
            mine = prices.setdefault(self.ticker, {})
            if valid:
                mine[key] = text
            else:
                mine.pop(key, None)
            if not mine:
                prices.pop(self.ticker, None)
            order = self.order()
            if key == "exit" and order and order.get("state") in ("buy_open", "bought"):
                if valid:
                    self.atm.set_sell_at(order, float(text))    # Sell ATM sells at it
                elif order.get("exit"):
                    self.atm.set_sell_at(order, order["exit"])  # unpinned: the plan's exit again
            if key in ("exit", "stop"):
                self.atm.update_live(self.ticker)
        self.refresh()

    def edit_done(self, key=None, edit=None):
        """Enter or leaving the box. A price typed as 1 or more is a count of 0.0001 steps:
        12 -> 0.0012, 1 -> 0.0001 (these stocks trade under $1)."""
        if key in ("size", "entry") and edit is not None and self.drafting() is not None:
            self.draft_done(key, edit)
            return
        e = self.selling(key) if edit is not None else None
        if e:                                   # the open sell's new exit: shown as a price (12 -> 0.0012)
            v = self.draft_number(edit.text(), price=True)
            text = price_text(format(Decimal(str(v)), "f")) if v else ""
            edit.setText(text)
            self.set_sell_draft(e, text)
            return
        if edit is not None and key != "size":
            try:
                typed = Decimal(edit.text().strip())
            except InvalidOperation:
                typed = None
            if typed is not None and typed >= 1:
                text = format(typed * PRICE_STEP, "f")
                edit.setText(text)
                self.typed(key, text)
            if edit.text().strip() and edit.text() != price_text(edit.text()):
                edit.setText(price_text(edit.text()))      # 0.05 -> 0.0500
        self.save_settings(self.settings)
        self.refresh()

    # ---- the ATM trade of the ticker shown (atm.json, through self.atm) ----
    def order(self, ticker=None):
        return self.atm.active(ticker or self.ticker)

    @staticmethod
    def style_button(button, name, text):
        bang = text.startswith("! ")            # "! Sell Partial": the "!" a yellow icon before the text
        button.setText(text[2:] if bang else text)
        button.setIcon(red_x_icon() if name == "cl_cancel" else red_bang_icon("#f2c200") if bang else QIcon())   # Cancel Buy / Sell
        if button.objectName() != name:
            button.setObjectName(name)
            button.style().unpolish(button)     # the panel's style sheet picks the new name
            button.style().polish(button)

    def update_trade_buttons(self):
        e = self.order()
        state = e.get("state") if e else None
        busy = self.atm.busy(e, self.ticker)
        can = self.trader is not None and bool(self.ticker) and not busy
        if state == "buy_open":
            self.style_button(self.buy_btn, "cl_cancel", "Cancel Buy")
            self.buy_btn.setEnabled(can)
        elif state == "bought":         # more of it, for the same trade
            self.style_button(self.buy_btn, "cl_buy", "Buy More")
            self.buy_btn.setEnabled(can and not e.get("rebuy") and not e.get("partial_sell"))
        else:
            self.style_button(self.buy_btn, "cl_buy", "Buy ATM")
            self.buy_btn.setEnabled(can and state is None)
        partial = state == "buy_open" and bool(e.get("filled"))      # partly bought: those can be sold
        if state == "sell_open":
            self.style_button(self.sell_btn, "cl_cancel", "Cancel Sell")
            self.sell_btn.setEnabled(can)
        elif partial:
            self.style_button(self.sell_btn, "cl_sell", "! Sell Partial")
            self.sell_btn.setEnabled(can and not e.get("partial_sell"))
        else:
            self.style_button(self.sell_btn, "cl_sell", "Sell ATM")
            self.sell_btn.setEnabled(can and state == "bought")
        self.done_btn.setVisible(state == "sold")
        self.bid_btn.setVisible(state == "bought")
        new = self.drafting()
        draft = self.draft_values(new) if new is not None else None
        self.update_btn.setVisible(bool(draft and draft[3]))
        self.update_btn.setEnabled(can)
        if draft and draft[3]:
            price, n, size = draft[:3]
            self.update_btn.setToolTip(f"Change the buy order to {n:,} @ {fmt_price(price)} (${size:,.2f}), "
                                       "after you approve it")
        selling = self.selling("exit")
        new_exit = self.sell_draft_price(selling)
        if selling is None:
            self.sell_draft = {}
        show = bool(new_exit and abs(new_exit - selling["sell_price"]) > 1e-9)
        self.update_sell_btn.setVisible(show)
        self.update_sell_btn.setEnabled(can)
        if show:
            self.update_sell_btn.setToolTip(f"Change the sell order from {fmt_price(selling['sell_price'])} to "
                                            f"{fmt_price(new_exit)}, after you approve it")
        self.bid_btn.setEnabled(can)
        if self.trader is None:
            self.buy_btn.setToolTip("Needs the Schwab login")
            self.sell_btn.setToolTip("Needs the Schwab login")
        else:
            self.buy_btn.setToolTip(
                "Cancel the buy order (after you approve it)" if state == "buy_open" else
                "Buy more of it for this ATM trade (after you approve it): the trade waits for the buy again, "
                "then owns all of them at the average price" if state == "bought" else
                "Send a GTC limit buy at the entry price for the amount of stocks, after you approve it"
                if state is None else "There is an ATM trade for this ticker (Monitor tab)")
            self.sell_btn.setToolTip(
                "Cancel the sell order, to change its price or keep the stocks longer" if state == "sell_open" else
                f"Sell the {e['filled']:,} bought so far (or fewer): the rest of the buy is canceled first, then the "
                "sell goes out, after you approve it" if partial else
                "Sell all the stocks at the exit price, GTC, after you approve it" if state == "bought" else
                "Once the stocks are bought")
        text = ""
        if e and e.get("sending"):
            lock = e["sending"]
            text = (f"Selling from Telegram ({lock.get('who', 'a phone')}): {lock.get('shares', 0):,} @ "
                    f"{fmt_price(lock.get('price'))}...")
        elif self.ticker in self.atm.sending or (e and e["id"] in self.atm.sending):
            text = "Sending the order to Schwab..."
        elif e and e["id"] in self.atm.canceling:
            text = "Canceling the order..."
        elif e:
            # the trade's order as Schwab's latest list of the account's orders has it (every app gets that list
            # at once; the trade itself is updated by the main server's app, a moment later): its status and fills
            o = self.atm.live_order(e)
            status = (o or {}).get("status") or e.get("status", "")
            status = f" ({status_word(status).upper()})" if status else ""
            left = self.atm.remaining(e)

            def times_text(side):
                """" (2 orders)(1 today)": the orders the fills were in (an order again on another day counts as
                another: one commission each), and those filled today."""
                orders = self.atm.commissions_count(e, side)
                today = (self.atm.fills_today_avg(e, side) or (0,))[0]
                return (f" ({orders} order{'s' if orders != 1 else ''})" if orders else "") + (
                    f"({today} today)" if today else "")

            def at_price(side, entry=None):
                """" @ 0.0124 (avg)": the fills' average price (" (avg)" when they were at different prices); the buys:
                the trade's entry (its average, as "You own ... @" and its profit have it)."""
                found = self.atm.fills_today_avg(e, side)
                price = entry if entry else found[1] if found else None
                if not price:
                    return ""
                return f" @ {fmt_price(price)}" + (" (avg)" if found and found[2] else "")

            def bought_line(n):
                """"Bought so far (2 orders)(2 orders today): 8,150 of 10,150 @ 0.0124 (avg)" (the buy's fills, its
                earlier orders' too)."""
                times = times_text("BUY")
                return (f"Bought so far{times}: {n:,} of {e['shares']:,}" + at_price("BUY", e.get("entry"))
                        + (" ✔" if n >= e["shares"] else ""))
            if state == "buy_open":
                bought = max(int(e.get("filled") or 0), int(e.get("filled_prev") or 0) + int((o or {}).get("filled") or 0))
                # the shares still waiting in the order, and below: all those bought so far (earlier orders too)
                text = (f"Buy order open{status}: {max(e['shares'] - bought, 0):,} @ {fmt_price(e['entry'])} GTC\n"
                        + bought_line(bought))
                # (new values typed: shown in the right column, sent with Update Buy; no line here)
            elif state == "bought":
                text = (f"You own {left:,} @ {fmt_price(e['entry'])}. Sell ATM sells them all at the exit price.\n"
                        + bought_line(int(e.get("filled") or 0)))
            elif state == "sell_open":
                sold = max(int(e.get("sell_filled") or 0), int((o or {}).get("filled") or 0))
                selling = min(e.get("sell_shares") or left, left)
                # the shares still waiting in the order, and below: those sold so far (of this order, and before)
                done = sold + int(e.get("sold") or 0)
                times = times_text("SELL")
                owned = int(e.get("filled") or 0)
                text = (f"Sell order open{status}: {max(selling - sold, 0):,} @ {fmt_price(e['sell_price'])} GTC\n"
                        + bought_line(owned) + "\n"
                        f"Sold so far{times}: {done:,} of {owned:,}" + (at_price("SELL") if done else "")
                        + (" ✔" if owned and done >= owned else ""))
            elif state == "sold":
                pl = self.atm.trade_pl(e)
                total = pl["net"] if pl else self.atm.profit(e)
                money = usd_signed(total)
                src = (" (Schwab)" if pl["source"] == "schwab" else " (estimated)") if pl else ""
                fees = f" after ${pl['fees']:.2f} fees" if pl else " after both fees"
                text = f"Sold {e.get('filled', 0):,}: {money}{fees}{src}."
                # the orders the buy and the sell were filled in (a commission each)
                n = int(e.get("filled") or 0)
                text += (f"\nBought{times_text('BUY')}: {n:,}{at_price('BUY', e.get('entry'))}"
                         f"\nSold{times_text('SELL')}: {n:,}{at_price('SELL')}")
            # (the trade's notes aren't shown here: they're in its history, on hover)
        self.order_text = text          # (as plain text)
        # its lines as rich text: a line's check (all bought / sold) in green
        self.order_label.setTextFormat(Qt.TextFormat.RichText)
        rich = html.escape(text).replace("\n", "<br>").replace(
            "✔", f"<span style='color: {MARKS[OK][1]}; font-weight: bold'>✔</span>")
        if e and state == "sold":       # the result: green when a gain, red when a loss
            color = MARKS[OK][1] if total >= 0 else MARKS[BAD][1]
            rich = rich.replace(html.escape(money), f"<span style='color: {color}; font-weight: bold'>"
                                                    f"{html.escape(money)}</span>", 1)
        self.order_label.setText(rich)
        self.order_label.setVisible(bool(text))
        history = self.atm.history_text(e) if e else ""
        self.order_label.setToolTip(f"{e['ticker']}'s history (newest first):\n\n{history}" if history else "")

    def buy_clicked(self):
        e = self.order()
        if e and e.get("state") == "buy_open":
            self.atm.ask_cancel(self, e, "BUY")
        elif e and e.get("state") == "bought":
            self.buy_more()
        else:
            self.buy()

    def sell_clicked(self):
        e = self.order()
        if e and e.get("state") == "sell_open":
            self.atm.ask_cancel(self, e, "SELL")
        elif e and e.get("state") == "buy_open" and e.get("filled"):
            self.sell_partial()
        else:
            self.sell()

    def reset_done(self):
        e = self.order()
        if e and e.get("state") == "sold":
            self.atm.reset(e)
            self.load()         # the typed values went: the boxes show the automatic ones again

    def buy(self):
        ticker = self.ticker
        if not ticker or self.trader is None or self.order(ticker) or self.atm.busy(ticker=ticker):
            return
        self.evaluate()
        p, _ = self.last_plan
        if not p["entry"] or not p["shares"]:
            QMessageBox.warning(self, "Buy ATM", "There's no entry price or amount of stocks yet.")
            return
        self.refresh()
        passed, count = getattr(self, "score", (0, 0))
        if passed < count:
            box = QMessageBox(QMessageBox.Icon.Warning, "Buy ATM",
                              "לא כל הצ'ק ליסט ירוק,\nהאם אתה בטוח שאתה רוצה לשים פקודת קנייה?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            box.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            box.setInformativeText(f"{passed}/{count} ✔")
            box.setDefaultButton(QMessageBox.StandardButton.No)
            self.dialog = box               # for tests
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        total = p["shares"] * p["entry"]
        fee = self.fees.get(ticker, (None, ""))[0]
        rows = [("Ticker", ticker), ("Order", "BUY · Limit · GTC (good till canceled)"),
                ("Amount of stocks", f"{p['shares']:,}"), ("Limit price (entry)", fmt_price(p["entry"]))
                ] + fee_rows(fee, total)
        self.fee_reader.load_accounts()
        account = self.fee_reader.order_tail()
        if not account:
            QMessageBox.warning(self, "Buy ATM", "Choose the account for orders on the Monitor tab first."
                                if self.fee_reader.accounts else "Can't read the Schwab accounts. Check the Schwab login.")
            return
        aon = aon_box()
        rows.append(("Fill", aon))
        if not confirm_order(self, self.atm.account_name(account),
                             f"Buy {p['shares']:,} {ticker} at {fmt_price(p['entry'])}?", rows, "Send BUY order"):
            return
        snapshot = self.atm_snapshot(p, account)
        snapshot["aon"] = aon.isChecked()
        self.atm.send_buy(snapshot)

    def atm_snapshot(self, p, account):
        """The plan and the market now (Level I and the full Level II), for atm.json.
        account: the last 3 digits of the account it goes to (the full number is never kept)."""
        fee = self.fees.get(self.ticker, (None, ""))[0]
        top = lambda book: (book[0][1], book[0][2]) if book else (None, None)
        bid = (self.bid, self.bid_size) if self.bid else top(self.bids)
        ask = (self.ask, self.ask_size) if self.ask else top(self.asks)
        book = lambda rows: [[mm, price, size] for mm, price, size, *_ in rows]
        return {"ticker": self.ticker, "account": account, "size": self.last_plan[1],
                "entry": p["entry"], "exit": p["exit"], "shares": p["shares"],
                "usd": round(p["shares"] * p["entry"], 2), "safety-net": p["stop"], "fee": fee,
                "time": datetime.now().isoformat(timespec="seconds"),
                "bid": list(bid), "ask": list(ask),
                "level2": {"bids": book(self.bids), "asks": book(self.asks)}}

    def sell(self):
        e = self.order()
        if not e or e.get("state") != "bought":
            return
        self.evaluate()
        p, _ = self.last_plan
        self.atm.ask_sell(self, e, p["exit"] or e.get("sell_at"), self.fees.get(self.ticker, (None, ""))[0])

    def buy_more(self):
        """Buy More: at the bid + 1 tick now (else the trade's entry); the shares: those the plan left unbought,
        else as many as the position $ buys."""
        e = self.order()
        if not e or e.get("state") != "bought":
            return
        self.evaluate()
        p, usd = self.last_plan
        start = (p.get("auto") or {}).get("entry") or e["entry"]
        left = int(e.get("shares") or 0) - int(e.get("filled") or 0)
        default = left if left > 0 else max(1, round((self.default_usd() or POSITION_USD) / start))
        self.atm.ask_buy_more(self, e, start, default, self.fees.get(self.ticker, (None, ""))[0])

    def sell_partial(self):
        """! Sell Partial: the stocks bought so far, at the exit now (the plan's, or changed here)."""
        e = self.order()
        if not e or e.get("state") != "buy_open" or not e.get("filled"):
            return
        self.evaluate()
        p, _ = self.last_plan
        self.atm.ask_sell_partial(self, e, p["exit"] or current_exit(e), self.fees.get(self.ticker, (None, ""))[0])

    def sell_on_bid(self):
        """The Sell ATM dialog at the best price the bids fill all the stocks at."""
        e = self.order()
        if not e or e.get("state") != "bought":
            return
        shares = self.atm.remaining(e)
        price, total = bid_fill(self.bids, shares)
        if price is None:
            QMessageBox.warning(self, "Sell on Bid", f"No Level II bids for {self.ticker} yet.")
            return
        if total >= shares:
            how = f"The bids from the best down to {fmt_price(price)} take {total:,} ≥ {shares:,}"
        else:
            how = (f"All the bids add up to {total:,} only, less than {shares:,}: "
                   f"at the lowest bid, the rest waits there")
        self.atm.ask_sell(self, e, price, self.fees.get(self.ticker, (None, ""))[0],
                          ("Limit price (on the Bid)", how))

    # ---- the fee from Schwab, once per ticker a session ----
    def read_fee(self, ticker=None):
        """Asks Schwab for the ticker's fee (the checklist's ticker), once a session."""
        ticker = ticker or self.ticker
        if self.fee_reader and ticker and ticker not in self.fees:
            self.fees[ticker] = (None, "Reading the fee from Schwab...")
            self.fee_reader.request(ticker)

    def shared_fee_read(self, ticker):
        """(a hidden copy) The app's panel read a fee: shown here too when it's this ticker's."""
        if ticker == self.ticker:
            self.refresh()

    def fee_read(self, ticker, fee, details):
        self.fees[ticker] = (fee, details)
        self.fee_changed.emit(ticker)
        if ticker == self.ticker:
            self.refresh()
        self.atm.market_changed.emit(ticker)        # the Monitor tab's orders show it

    # ---- data in ----
    def set_chart(self, ticker, rows, market_days, rng, period):
        """The chart's candles, the last VOL_DAYS market days, the bounce range (or None) and
        the chart's Averaging days."""
        if ticker != self.ticker:
            self.ticker = ticker
            self.bid = self.ask = None
            self.bid_size = self.ask_size = None
            self.bids, self.asks, self.l2_live = [], [], False
        self.rows, self.market_days, self.range, self.period = rows, market_days, rng, period
        self.read_fee()
        self.refresh()

    def set_reloaders(self, ticker, found):
        """{yellow market maker: its reload events} of the ticker (the Level II panel's): diluters."""
        self.reloaders, self.reloaders_of = found, ticker
        if ticker == self.ticker:
            self.refresh()

    def set_quote(self, ticker, q):
        if ticker == self.ticker:
            self.bid = q.get("BID_PRICE") or self.bid
            self.ask = q.get("ASK_PRICE") or self.ask
            self.bid_size = q.get("BID_SIZE") or self.bid_size
            self.ask_size = q.get("ASK_SIZE") or self.ask_size
            self.refresh()

    def set_book(self, ticker, bids, asks):
        if ticker == self.ticker:
            self.bids, self.asks, self.l2_live = bids, asks, True
            self.refresh()

    def clear_live(self):
        self.bid = self.ask = None
        self.bid_size = self.ask_size = None
        self.bids, self.asks, self.l2_live = [], [], False
        self.refresh()

    # ---- manual marks, per ticker: {item: "ok" or "bad"}, an empty one isn't kept ----
    def ticked(self):
        saved = self.settings.get("checklist_ticks", {}).get(self.ticker, {})
        if isinstance(saved, list):         # before v0.6.6: a list of the checked items
            saved = dict.fromkeys(saved, OK)
        return dict(saved)

    def toggle(self, item):
        if not self.ticker:
            return
        ticks = self.ticked()
        old = ticks.pop(item.text, EMPTY) if item.text != item.key else EMPTY    # saved by its text before
        state = NEXT_MARK[ticks.get(item.key, old)]
        if state == EMPTY:
            ticks.pop(item.key, None)
        else:
            ticks[item.key] = state
        all_ticks = self.settings.setdefault("checklist_ticks", {})
        if ticks:
            all_ticks[self.ticker] = ticks
        else:
            all_ticks.pop(self.ticker, None)
        self.save_settings(self.settings)
        self.refresh()

    # ---- the rules ----
    def evaluate(self):
        """{key: (state, value text, tooltip)} for the auto and info keys."""
        bid = self.bid or (self.bids[0][1] if self.bids else None)
        ask = self.ask or (self.asks[0][1] if self.asks else None)
        own = self.overrides()
        default = self.default_usd()
        usd = own.get("size", default)
        fee, fee_details = self.fees.get(self.ticker, (None, "No ticker"))
        fee_used = FEE_USD if fee is None else fee
        order = self.order()
        shares = ((self.atm.remaining(order) if order.get("state") in ("bought", "sell_open") else 0)
                  or order.get("filled") or order.get("shares")) if order else None
        if order:
            own["entry"] = order["entry"]       # the buy's price: exit and stop are worked out from it
        selling = self.selling("exit")
        if selling:                             # the open sell: its price, or the new exit typed
            own["exit"] = self.sell_draft_price(selling) or selling["sell_price"]
        elif order and order.get("state") in ("buy_open", "bought") and not own.get("exit"):
            own["exit"] = current_exit(order)   # not pinned: the trade's exit (as the Monitor tab's New Price)
        new = self.drafting()
        if new is not None:
            price, n, _, changed = self.draft_values(new)
            if changed:                     # the new values typed beside the buy's: what they would give
                own["entry"], shares = price, n
        pinned_shares = int(own["shares"]) if not order and own.get("shares") else None
        if pinned_shares:               # the amount pinned for this ticker: the $ follows it
            shares = pinned_shares
        p = plan(bid, ask, self.bids, usd, own.get("entry"), own.get("exit"), own.get("stop"), fee_used, shares)
        if pinned_shares and p["entry"]:
            usd = round(pinned_shares * p["entry"], 6)
        self.last_plan = (p, usd)       # for the trade plan the ticker copies
        auto = p["auto"]
        pct = lambda v: NA if v is None else f"{v:+.1f}%"
        price = lambda v: NA if v is None else fmt_price(v)
        usd_text = lambda v: NA if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
        typed = "Type your own value, or clear it to use the automatic one."
        # For the typed-over keys the value is the automatic one, shown when nothing is typed
        r = {
            "ticker": (INFO, self.ticker or NA, ""),
            "entry": (INFO, price(auto["entry"]), "Trips: the Bid. Dubs and up: the Bid + 0.0001. " + typed),
            "size": (INFO, usd_plain(usd) if pinned_shares else usd_plain(default),
                     f"The amount pinned x the entry (unpin the amount, or pin this, to go by the $)" if pinned_shares
                     else f"The default {usd_plain(default)} (the Settings tab), or pin your own for all tickers"),
            "shares": (INFO, f"{p['shares']:,}" if p["shares"] else NA,
                       "From the ATM trade (Monitor tab)" if order else f"round({usd:g} / entry)"),
            "exit": (INFO, price(current_exit(order)), "The ATM trade's exit now (as the Monitor tab's New Price; "
                     "Sell ATM sells at it). " + typed) if order and order.get("state") in ("buy_open", "bought") else
                    (INFO, price(auto["exit"]), "Trips: the Ask. Dubs and up: the Ask - 0.0001. " + typed),
            "order": (INFO, "GTC", ""),
            "stop": (INFO, price(auto["stop"]),
                     f"The highest bid where the bids add up to {COVER_X}x our amount of stocks. " + typed),
            "profit": (INFO, pct(p["profit"]), "(exit - entry) / entry"),
            "loss": (INFO, pct(-p["loss"]) if p["loss"] is not None else NA, "(entry - stop) / entry"),
            "fee": ((UNKNOWN, NA, f"Not read from Schwab, ${FEE_USD:.2f} is used: {fee_details}")
                    if fee is None else
                    (OK if fee <= FEE_USD + 0.001 else BAD, f"${fee:.2f}",
                     fee_details + ("" if fee <= FEE_USD + 0.001 else f" More than ${FEE_USD:.2f}!"))),
            "profit_gross": (INFO, usd_text(p["profit_gross"]), "amount of stocks x (exit - entry), without the fees"),
            "loss_gross": (INFO, usd_text(p["loss_gross"]), "-amount of stocks x (entry - stop), without the fees"),
            "profit_usd": (INFO, usd_text(p["profit_usd"]),
                           f"amount of stocks x (exit - entry) - the buy's ${fee_used:.2f} fee - the sell's (${fee_used:.2f}, never more than the sale brings)"),
            "loss_usd": (INFO, usd_text(p["loss_usd"]),
                         f"-amount of stocks x (entry - stop) - the buy's ${fee_used:.2f} fee - the sell's (${fee_used:.2f}, never more than the sale brings)"),
            "ratio": (INFO, f"1:{p['ratio']:.2f}" if p["ratio"] is not None else NA, "1 : profit % / loss % (the fees are left out)"),
        }

        r["range"] = range_check(self.rows, self.range)
        r["min_shares"] = min_shares_check(p["shares"], p["entry"])
        if not self.l2_live:
            for key in ("l2_live", "no_diluter", "buyers", "cushion"):
                r[key] = (NOT_AVAILABLE if key == "no_diluter" else UNKNOWN, "", "No Level II for this ticker yet")
            r["l2_live"] = (BAD, "", "No Level II for this ticker yet")
        else:
            r["l2_live"] = (OK, "", "")
            r.update(l2_checks(self.bids, self.asks, bid, ask, p,
                               self.reloaders if self.reloaders_of == self.ticker else None))
        r.update(volume_checks(self.rows, self.market_days, self.period, p["shares"]))
        spike_bad = r["no_spike"][0] == BAD
        red_bad = r["no_diluter"][0] == BAD
        if spike_bad or red_bad:
            r["no_dilution"] = (BAD, "", " · ".join(x[2] for x, bad in ((r["no_spike"], spike_bad),
                                                                        (r["no_diluter"], red_bad)) if bad))
        elif {UNKNOWN, NOT_AVAILABLE} & {r["no_spike"][0], r["no_diluter"][0]}:
            r["no_dilution"] = (UNKNOWN, "", "Needs the candles and the Level II")
        else:
            r["no_dilution"] = (OK, "", "")
        return r

    @staticmethod
    def risk_of(ticker):
        """The ticker's risk by its last letter: (level: 1 risky / 2 dangerous, case, what it means) or None. The
        app sets it (its TICKER_RISKS), and risk_icons (level -> the chart's sign)."""
        return None

    risk_icons = {}

    def plan_text(self):
        """The trade plan, as text to paste: the values the checklist shows now."""
        if not self.ticker:
            return ""
        self.evaluate()
        p, usd = self.last_plan
        e = self.order()
        if e and e.get("entry"):        # an ATM trade: its plan (the fixed cells), not the panel's live one
            usd = e.get("size") or e.get("usd") or usd
            fee = FEE_USD if e.get("fee") is None else e["fee"]
            p = plan(None, None, [], usd, e["entry"], e.get("exit"), e.get("safety-net"), fee, e.get("shares"))
        return self.format_plan(p, usd)

    def new_plan_text(self):
        """An open buy (state 2): the plan with the new values, as the right column shows them now."""
        e = self.drafting()
        if not self.ticker or e is None:
            return ""
        self.evaluate()             # (the right column: the new entry and amount, the live exit and stop)
        p, _ = self.last_plan
        _, _, size, _ = self.draft_values(e)
        return self.format_plan(p, size)

    def format_plan(self, p, usd):
        def num(v, fmt):
            return NA if v is None else fmt(v)
        plain = lambda v: f"{v:,.2f}".replace(",", "").rstrip("0").rstrip(".")     # 150.00 -> 150, 23.50 -> 23.5
        lines = [
            "תכנית פעולה",
            f"טיקר : {self.ticker}",
            f"פוזיציה: ${plain(usd)}",
            f"כניסה: {num(p['entry'], fmt_price)}",
            f"יציאה: {num(p['exit'], fmt_price)}",
            f"כמות מניות: {num(p['shares'], str)}",
            f"כרית בטחון: {num(p['stop'], fmt_price)}",
            f"רווח פוטנציאלי: {num(p['profit'], lambda v: f'{v:.1f}%')}",
            f"הפסד פוטנציאלי: {num(p['loss'], lambda v: f'{v:.1f}%')}",
            f"יחס רווח להפסד: {num(p['ratio'], lambda v: f'1:{v:.2f}')}",
            f"רווח דולרי אחרי עמלות : {num(p['profit_usd'], lambda v: plain(v) + '$')}",
        ]
        return chr(10).join(lines)      # one line each

    def locked_texts(self, e):
        """The fixed cells' texts: the plan sent with Buy ATM, and what it gives - its exit as planned at the buy
        (exit-original: kept when the sell order's price was changed since; the trade's exit follows the order)."""
        fee = FEE_USD if e.get("fee") is None else e["fee"]
        size = e.get("size") or e.get("usd")
        p = plan(None, None, [], size, e["entry"], e.get("exit-original") or e.get("exit"), e.get("safety-net"), fee,
                 e.get("shares"))
        price = lambda v: NA if v is None else fmt_price(v)
        pct = lambda v: NA if v is None else f"{v:+.1f}%"
        usd = lambda v: NA if v is None else usd_signed(v)
        return {"size": NA if size is None else f"${size:,.2f}".replace(".00", ""),
                "entry": price(p["entry"]), "exit": price(p["exit"]), "stop": price(p["stop"]),
                "profit": pct(p["profit"]), "loss": pct(-p["loss"]) if p["loss"] is not None else NA,
                "profit_gross": usd(p["profit_gross"]), "loss_gross": usd(p["loss_gross"]),
                "profit_usd": usd(p["profit_usd"]), "loss_usd": usd(p["loss_usd"]),
                "ratio": f"1:{p['ratio']:.2f}" if p["ratio"] is not None else NA}

    def fills_inputs(self):
        """The fill chances' (ticker, entry, exit, shares): an ATM trade's own (its exit now), else the plan shown."""
        if not self.ticker:
            return None
        e = self.order()
        if e and e.get("entry"):
            return self.ticker, e["entry"], current_exit(e) or e.get("exit"), int(e.get("shares") or 0)
        p = (getattr(self, "last_plan", None) or ({}, None))[0] or {}
        if not p.get("entry") or not p.get("exit") or not p.get("shares"):
            return None
        return self.ticker, p["entry"], p["exit"], int(p["shares"])

    def update_fills(self):
        """The fill chances worked out again (in a thread: the record's files) when the ticker, the entry, the exit or
        the shares changed, and every FILLS_AGAIN_S (today's record grows)."""
        import otc_fills
        got = self.fills_inputs()
        if got is None:
            self.fills_key = None
            self.fills_summary, self.fills_days = "", []
            self.fills_label.hide()
            return
        key = (got[0], round(got[1], 6), round(got[2], 6), got[3])
        if key == self.fills_key and time.time() - self.fills_at < FILLS_AGAIN_S:
            return
        self.fills_key, self.fills_at = key, time.time()

        def work():
            try:
                found = otc_fills.estimate(*key)
            except Exception as ex:      # (never stops the app)
                found = ([], {"days": 0, "bought": 0, "round_trips": 0, "error": f"{type(ex).__name__}: {ex}"})
            self._fills_done.emit(key, found)
        threading.Thread(target=work, name="fills", daemon=True).start()

    def show_fills(self, key, found):
        import otc_fills
        if key != self.fills_key:
            return              # (older inputs)
        rows, summary = found
        self.fills_summary = otc_fills.summary_text(summary)
        self.fills_days = otc_fills.day_lines(rows, key[1], key[2], key[3])
        self.fills_label.setText(self.fills_summary)
        self.fills_label.setToolTip(
            f"Buy {key[3]:,} @ {fmt_price(key[1])}, then sell @ {fmt_price(key[2])} - replayed on each recorded day "
            "(US Eastern times; the shares already shown at the price: ahead in the queue):\n\n"
            + ("\n".join(self.fills_days) or "No recorded days.")
            + "\n\nAn estimate: trades between two updates come merged, shares ahead that left still count.")
        self.fills_label.show()

    def refresh(self):
        # (the step circles: as Schwab's latest orders show it, at once)
        self.steps_view.set_trade(self.atm.live_view(self.order()) if self.ticker else None)
        if not self.widgets:
            return
        results = self.evaluate()
        order = self.order()
        self.update_trade_buttons()
        self.update_fills()             # (the plan just worked out: its entry, exit and shares)
        fixed = self.locked_texts(order) if order else {}
        changeable = self.can_change_order(order)
        if self.new_plan_btn is not None:
            self.new_plan_btn.setVisible(changeable)
        for key, oe in self.order_edits.items():
            oe.setVisible(changeable)
            if changeable and not oe.hasFocus() and not self.changing_order:
                oe.setText(self.order_edit_text(key, order))
        for key, lock in self.locked.items():
            lock.setVisible(bool(order) and not (changeable and key in self.order_edits))
            if order:
                lock.setText(fixed.get(key, NA))
                lock.setToolTip("Fixed: the plan sent with Buy ATM" + (
                    "" if key in ("entry", "size") else ". Beside it: now, from the live prices."))
        for key in LOCKED + ("shares", "fee"):     # no copy buttons by the live cells once sent
            if isinstance(self.boxes.get(key), CopyBox):
                self.boxes[key].set_copyable(not order)
        if isinstance(self.boxes.get("ticker"), CopyBox):   # an ATM trade: its plan's copy always there
            self.boxes["ticker"].set_always(bool(order))
            self.boxes["ticker"].button.setToolTip("Copy the trade plan (the order's)" if order else "Copy")
        # ...and the values without a fixed cell start where the fixed cells' values do
        for item, _, _, value in self.widgets:
            if item.key == "ticker" and isinstance(value, QLabel):
                value.setIndent(1 if order else -1)     # (beside its copy button)
            elif item.key in ("shares", "fee") and isinstance(value, QLabel):
                value.setIndent(LOCKED_INDENT if order else -1)
            elif item.key in LOCKED and isinstance(value, QLabel) and not isinstance(value, QLineEdit):
                value.setIndent(LIVE_INDENT if order else -1)     # in line with the live price boxes
        for key in ("entry", "size"):       # only the fixed cell once sent (an open buy: new values beside it)
            if key in self.boxes:
                self.boxes[key].setVisible(not order or changeable)
            tools = self.price_tools.get(key, ())
            for i, w in enumerate(tools):
                w.setVisible(not order or (changeable and i == 0))     # the pin: only before Buy ATM
        draft = self.draft_values(order) if changeable else None
        if draft is None:
            self.draft = {}
        for key, tools in self.draft_tools.items():
            before = not order and key != "entry"       # the $ and the amount before a trade, too
            for w in tools:
                w.setVisible(changeable or before)
            if before and tools:
                pin = tools[-1]
                self.show_pin(pin, self.plan_pinned(key))
                default = usd_plain(self.default_usd())
                follows = {"size": f"the default {default} (the Settings tab)",
                           "shares": "the position $ / the entry"}[key]
                kept = {"size": "this $ is kept, for all tickers",
                        "shares": "this amount is kept for this ticker (the $ follows it)"}[key]
                pin.setToolTip(f"Pinned: {kept}.\nClick to unpin: back to {follows}."
                               if pin.property("pinned") else
                               f"Not pinned: {follows}.\nClick to pin it as it is now (typing or + / - pins it too).")
            if changeable and tools:
                pin = tools[-1]
                self.show_pin(pin, bool(self.draft.get(key)))
                original = {"size": f"${self.order_size(order):,.2f}", "entry": fmt_price(order["entry"]),
                            "shares": f"{order['shares']:,}"}[key]
                follows = {"entry": f"the order's {original}",
                           "size": f"the order's {original}, or the new amount of stocks x the price",
                           "shares": "the new position $ / the price"}[key]
                pin.setToolTip(f"Pinned: this new value is kept.\nClick to unpin: back to {follows}."
                               if pin.property("pinned") else
                               f"Not pinned: {follows}.\nClick to pin it as it is now (typing or + / - pins it too).")
        ticks = self.ticked()
        manual, solvers = {}, {}      # the manual items' marks and texts, by their keys
        for item, *_ in self.widgets:
            if item.kind == "manual":
                manual[item.key] = ticks.get(item.key, ticks.get(item.text, EMPTY))
                solvers[item.key] = item.text
        passed = total = 0

        def ticker_mark(mark, text):
            """The ticker's mark: a red ✘ when dangerous (its 5th letter: bankruptcy, delinquent...), with the chart's
            red sign beside its name; else ✔ (a risky one, foreign: a pass, no sign here: shown elsewhere)."""
            nonlocal passed, total
            risk = self.risk_of(self.ticker) if self.ticker else None     # (level, case, what it means)
            if risk and risk[0] < 2:
                risk = None         # (only the dangerous ones here)
            if not self.ticker:
                st, why = UNKNOWN, "No ticker"
            elif not risk:
                st, why = OK, "Not one of the dangerous kinds (by its 5th letter)"
            else:
                st, why = BAD, f"DANGEROUS: {self.ticker[-1]} = {risk[1]}: {risk[2]}"
            set_mark(mark, st)
            for w in (mark, text):
                w.setToolTip(why)
            sign = getattr(self, "ticker_risk_sign", None)
            if sign is not None:
                icon = (self.risk_icons or {}).get(risk[0]) if risk else None
                sign.setVisible(icon is not None)
                if icon is not None:
                    sign.setPixmap(icon.pixmap(16, 16))
                    sign.setToolTip(why)
            total += 1
            passed += st == OK

        def shares_mark(mark, text):
            """The amount of stocks' mark: at least MIN_SHARES (Pennies: MIN_SHARES_PENNY)."""
            nonlocal passed, total
            st, _v, why = results.get("min_shares", (UNKNOWN, "", ""))
            set_mark(mark, st)
            for w in (mark, text):
                w.setToolTip(why)
            total += 1
            passed += st == OK
        for item, mark, text, value in self.widgets:
            if item.kind == "manual":
                state, val, tip = ticks.get(item.key, ticks.get(item.text, EMPTY)), "", "Click: empty → ✔ → ✘ → empty"
            else:
                state, val, tip = results.get(item.key, (UNKNOWN, "", f"No rule called \"{item.key}\""))
                if state == BAD and item.solved_by and manual.get(item.solved_by) == OK:
                    state = SOLVED
                    tip = (tip + " · " if tip else "") + f"Solved: \"{solvers.get(item.solved_by, item.solved_by)}\" is checked"
            if isinstance(value, QLineEdit) and draft is not None and item.key in ("size", "entry", "shares"):
                # the open buy's new values: typed (orange), or what follows from them (white)
                price, n, size, changed = draft
                typed_text = self.draft.get(item.key, "")
                if item.key == "size" and typed_text:
                    typed_text = f"${typed_text}"
                elif item.key == "shares" and typed_text.replace(",", "").isdigit():
                    typed_text = f"{int(typed_text.replace(',', '')):,}"
                value.setReadOnly(False)
                value.setEnabled(True)
                value.setStyleSheet("")
                value.setPlaceholderText({"size": f"${size:,.2f}", "entry": fmt_price(price), "shares": f"{n:,}"}[item.key])
                # (a box with the focus keeps what's being typed in it, but not a value the fixed view left in it
                # while the order was being sent: that would count as typed when the box is left)
                if value.text() != typed_text and (not value.hasFocus() or not self.draft.get(item.key)):
                    value.setText(typed_text)
                value.setToolTip({"size": "New position $ for the buy order: the amount of stocks follows",
                                  "entry": "New limit price for the buy order: the amount of stocks follows (for "
                                           "the same position $). 12 = 0.0012",
                                  "shares": "New amount of stocks for the buy order: the position $ follows"}[item.key]
                                 + ". Then Update Buy at the top changes the order (after you approve it). "
                                   "Clear the boxes to keep the order as it is.")
                if item.key == "shares":
                    shares_mark(mark, text)
                continue
            if isinstance(value, QLineEdit) and item.key == "shares":
                # the trade's amount once sent (fixed, in the order colour), else the plan's: type to change
                value.setReadOnly(bool(order))
                value.setEnabled(bool(self.ticker))
                value.setPlaceholderText(val)
                # fixed: no box around it, as the other fixed cells (the left column's)
                value.setStyleSheet(f"QLineEdit {{ color: {ORDER_COLOR}; font-weight: bold; background: transparent; "
                                    f"border: none; padding: 1px 0px 1px 5px; }}" if order else "")
                if order:
                    value.setText(val)
                elif not value.hasFocus():
                    amount = self.override_text("shares")
                    value.setText(f"{int(float(amount)):,}" if amount else "")
                value.setToolTip(tip if order else f"{tip}. Type an amount (or + / -) to pin it for this ticker: "
                                                   f"the position $ then follows it.")
                shares_mark(mark, text)
                continue
            if isinstance(value, QLineEdit):
                value.setPlaceholderText(val)
                state = order.get("state") if order else None
                # the exit once sold; while the sell order works, a new one changes the order
                locked = item.key == "exit" and state == "sold"
                selling = item.key == "exit" and state == "sell_open" and order.get("sell_price")
                if locked:
                    tip = "Sold at the sell order's price."
                elif selling:
                    tip = (f"The sell order's price: {fmt_price(order['sell_price'])}. Type a new exit (or + / -) to "
                           f"see what it gives; Update Sell at the top changes the order (it asks first). The pin "
                           f"(or clearing the box): back to the order's price. 12 = 0.0012")
                    val = fmt_price(order["sell_price"])
                    value.setPlaceholderText(val)
                elif order and item.key == "exit":
                    tip += " Sell ATM sells at it. Pinned, it's kept on every computer (and the Telegram Sell as planned)."
                value.setToolTip(tip)
                value.setReadOnly(locked)
                tools = self.price_tools.get(item.key)
                own = self.override_text(item.key)
                if selling:                     # the new exit typed (orange), else the order's price (white)
                    own = self.sell_draft.get("exit", "") if self.sell_draft_price(order) else ""
                if tools:
                    tools[0].setEnabled(not locked)
                    tools[1].setEnabled(not locked)
                    self.show_pin(tools[1], bool(own))
                value.setEnabled(bool(self.ticker) or item.key == "size")
                if item.key == "size":
                    own = f"${own}" if own and not self.override_text("shares") else ""   # with its sign
                else:
                    own = price_text(own)
                if not value.hasFocus() and value.text() != own:
                    value.setText(own)
                continue
            if item.kind == "info":
                value.setText(val)
                value.setToolTip(tip)
                if item.key == "shares":
                    value.setStyleSheet(f"color: {ORDER_COLOR};" if order else "")
                if item.key in ("profit", "profit_gross", "loss_gross", "profit_usd", "loss_usd"):
                    value.setStyleSheet("" if val in ("", NA) else
                                        f"color: {MARKS[OK if val.startswith('+') else BAD][1]};")
                elif item.key in ("loss", "ratio"):
                    # the loss: green when the profit is at least 3x it, else red; the ratio: green from 1:3
                    # (the ratio's ✔); both by the ratio's check, so they always agree
                    plan_now = self.last_plan[0]
                    good = target_state("ratio", plan_now["ratio"])[0]
                    if plan_now["ratio"] is None and plan_now["loss"] == 0 and (plan_now["profit"] or 0) > 0:
                        good = OK           # (no loss at all, and a profit)
                    value.setStyleSheet("" if val in ("", NA) or good == UNKNOWN else
                                        f"color: {MARKS[OK if good == OK else BAD][1]};")
                    if item.key == "loss":      # its mark: the ratio's (✔ when the profit is at least 3x it)
                        state = good if val not in ("", NA) else UNKNOWN
                        set_mark(mark, state)
                        for w in (mark, text):
                            w.setToolTip((f"{tip} · " if tip else "") + f"The profit at least {TARGETS['ratio']:g}x it")
                        total += 1
                        passed += state == OK
                        continue
                if item.key == "ticker":
                    ticker_mark(mark, text)
                    continue
                if item.key not in TARGETS:
                    continue
                # ✔ once it reaches its target, else ✘ (? while unknown)
                state, need = target_state(item.key, self.last_plan[0][item.key])
                tip = (f"{tip} · " if tip else "") + need
                set_mark(mark, state)
                for w in (mark, text):
                    w.setToolTip(tip)
                total += 1
                passed += state == OK
                continue
            if value is not None:
                value.setText(val)
                if item.key == "fee":
                    high = val.startswith("$") and float(val[1:]) > FEE_HIGH
                    value.setStyleSheet("color: #e3b23c;" if high else "")    # the section titles' color
            set_mark(mark, state)
            for w in (mark, text):
                w.setToolTip(tip)
            total += 1
            passed += state in (OK, SOLVED)
        self.summary.setText(f"{self.ticker} · {passed}/{total} ✔" if self.ticker else "")
        self.score = (passed, total)        # Buy ATM asks first when not all are ✔
