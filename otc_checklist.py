"""The trade checklist next to the chart: the items of checklist.txt in Hebrew, each with a green
check when its condition is met and a red X when it isn't. "auto" items are worked out from the
chart's candles and the live Level II, "manual" ones are clicked by hand (saved per ticker): empty,
then a check, then an X, then empty again, and
"info" ones show a value: the entry, exit and stop prices, the amount of stocks and so on.
The position size and the entry, exit and stop prices can be typed over: the position size is kept
for all tickers, the prices per ticker, and clearing a field brings back the automatic value."""

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from PyQt6.QtCore import QObject, QPointF, QTimer, QRect, QRectF, QRegularExpression, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QPolygonF,
                         QRegularExpressionValidator)
from PyQt6.QtWidgets import (QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QToolButton, QToolTip,
                             QVBoxLayout, QWidget)

from otc_chart import fmt_price, fmt_volume
from otc_level2 import MM_RED, MM_YELLOW

POSITION_USD = 150      # the position size, in $
FEE_USD = 6.95          # the fee for each buy and each sell, in $, until Schwab's is read
FEE_HIGH = 7.0          # a fee above this is shown in the section titles' yellow
COVER_X = 2             # the stop: bids for at least this many times our amount of stocks
VOL_DAYS = 30           # market days in the volume checks
SPIKE_X = 2.0           # a day with this many times the average volume is unusual
MAX_DEAD = 5            # most days without trades in VOL_DAYS for a steady volume
VOL_PART = 0.5          # the median volume must be at least this part of our amount of stocks
COPIED_MS = 1000        # how long the "Copied" note stays, in ms
RANGE_DAYS = 30         # calendar days the bounce range must have lasted
NA = "N/A"              # a value that isn't known (yet)
EDITABLE = ("size", "entry", "exit", "stop")     # info values that can be typed over
# Info values with a mark too: a ✔ once they reach their target (profit in %, ratio as 1:X)
TARGETS = {"profit": 25.0, "ratio": 3.0}
# Once Buy ATM was pressed these show the plan sent, fixed, in a cell of their own. Entry and size
# show only that; the others keep a live cell beside it (exit and stop typed or automatic, as before
# the buy; the gains and losses worked out from them)
LOCKED = ("size", "entry", "exit", "stop", "profit", "loss", "profit_usd", "loss_usd", "ratio")
LOCKED_W = 84             # px: the fixed cells' width
LIVE_INDENT = 6           # px: a live value's text, where the text in the live price boxes starts
LOCKED_INDENT = 8         # px: the fixed cells' left padding, for the values beside them to line up
PRICE_STEP = Decimal("0.0001")      # the +/- buttons; a typed whole number means that many steps (12 -> 0.0012)
ORDER_COLOR = "#4fc3f7"     # the plan's values once a BUY order was sent: fixed, not the live ones
ORDER_CHECK_S = 60          # how often open orders are checked at Schwab, in seconds
ORDERS_BACK_DAYS = 180      # how far back the account's orders are read
# Schwab order statuses after which an order no longer works
ORDER_DONE = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
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
MARKS = {OK: ("✔", "#1db31d"), BAD: ("✘", "#e0404f"), WARN: ("!", "#f0a020"), UNKNOWN: ("?", "#6c6c6c"),
         EMPTY: ("☐", "#8a8a8a"), SOLVED: ("✘", "#1db31d"), MAYBE: ("?", "#f3c400")}
NEXT_MARK = {EMPTY: OK, OK: BAD, BAD: EMPTY}     # a click on a manual item

PANEL_STYLE = """
#checklist, #checklist QScrollArea, #checklist #cl_body { background: #101214; }
#checklist QLabel { color: #d8d8d8; }
#checklist QLabel#cl_title { font-size: 12pt; font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_section { font-weight: bold; color: #e3b23c; padding-top: 8px; }
#checklist QLabel#cl_value { font-weight: bold; color: #ffffff; }
#checklist QLabel#cl_summary { color: #a8a8a8; }
#checklist QToolButton { color: #c8c8c8; border: none; }
#checklist QToolButton#cl_copy { border-radius: 3px; padding: 1px; }
#checklist QToolButton#cl_copy:hover { background: #2c3136; }
#checklist QToolButton#cl_step { background: #1d2125; border: 1px solid #3a4046; border-radius: 2px;
                                  padding: 0px; font-size: 11px; font-weight: bold; }
#checklist QToolButton#cl_step:hover { background: #2c3136; }
#checklist QToolButton#cl_step:pressed { background: #3a4046; }
#checklist QToolButton#cl_reset { border-radius: 3px; padding: 0px; font-size: 15px; }
#checklist QToolButton#cl_reset:hover { background: #2c3136; }
#checklist QToolButton#cl_reset:pressed { background: #3a4046; }
#checklist QLineEdit { background: #1b1e21; border: 1px solid #2c3136; border-radius: 3px;
                       padding: 1px 4px; font-weight: bold; }
#checklist QLineEdit:focus { border-color: #5a6670; }
#checklist QLineEdit:disabled { background: #101214; border-color: #1b1e21; }
#checklist QLineEdit[readOnly="true"] { background: #101214; }
#checklist QPushButton#cl_buy, #checklist QPushButton#cl_sell {
    color: #ffffff; font-weight: bold; border-radius: 4px; padding: 3px 14px; border: 1px solid transparent; }
#checklist QPushButton#cl_buy { background: #009300; }
#checklist QPushButton#cl_buy:hover { background: #00b300; }
#checklist QPushButton#cl_sell { background: #cf0508; }
#checklist QPushButton#cl_sell:hover { background: #e8262a; }
#checklist QPushButton#cl_cancel, #checklist QPushButton#cl_done {
    font-weight: bold; border-radius: 4px; padding: 3px 14px; border: 1px solid transparent; }
#checklist QPushButton#cl_cancel { background: #e3b23c; color: #111111; }
#checklist QPushButton#cl_cancel:hover { background: #f0c75a; }
#checklist QPushButton#cl_done { background: #6a4fb0; color: #ffffff; padding: 2px 10px; }
#checklist QPushButton#cl_done:hover { background: #7f62c8; }
#checklist QPushButton#cl_buy:disabled, #checklist QPushButton#cl_sell:disabled,
#checklist QPushButton#cl_cancel:disabled {
    background: #1d2125; color: #5c6268; border-color: #2c3136; }
#checklist QLabel#cl_order { color: #4fc3f7; }
#checklist QLabel#cl_locked { color: #4fc3f7; font-weight: bold; background: transparent;
                              border: none; padding: 1px 0px 1px 5px; }
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
    {entry, exit, shares, stop, profit, loss, ratio, profit_usd, loss_usd, auto}; parts that can't be worked out are None.
    A given entry, exit, stop or amount of stocks (an order's) is used instead of the automatic one;
    "auto" holds the automatic {entry, exit, stop} (the stop for the amount of stocks actually used)."""
    out = dict.fromkeys(("entry", "exit", "shares", "stop", "profit", "loss", "ratio", "profit_usd", "loss_usd"))
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
        total = 0
        for _, price, size, _ in bids:
            total += size
            if total >= COVER_X * out["shares"]:
                auto["stop"] = price
                break
    out["stop"] = stop if stop is not None else auto["stop"]
    if out["entry"] and out["exit"]:
        out["profit"] = (out["exit"] - out["entry"]) / out["entry"] * 100
    if out["entry"] and out["stop"] is not None:
        out["loss"] = (out["entry"] - out["stop"]) / out["entry"] * 100
    fees = 2 * fee          # one buy and one sell
    if out["shares"] and out["exit"]:
        out["profit_usd"] = out["shares"] * (out["exit"] - out["entry"]) - fees
    if out["shares"] and out["stop"] is not None:
        out["loss_usd"] = -out["shares"] * (out["entry"] - out["stop"]) - fees
    if out["profit"] is not None and out["loss"]:
        out["ratio"] = out["profit"] / out["loss"]
    return out


def volume_stats(rows, market_days, period):
    """(median, [unusual days], days without trades) from the candles (date, open, high, low,
    close, volume, ...). The median is the chart's MedianVolume: the last `period` days with
    trades. Unusual days and days without trades are counted over the given market days.
    None if there are no market days."""
    if not market_days or not rows:
        return None
    med = median([r[5] or 0.0 for r in rows[-period:]])
    by_day = {r[0]: r[5] or 0.0 for r in rows}
    vols = [by_day.get(d, 0.0) for d in market_days]
    spikes = [d for d, v in zip(market_days, vols) if med and v >= SPIKE_X * med]
    dead = sum(1 for v in vols if not v)
    return med, spikes, dead


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
    QToolTip.showText(QCursor.pos(), f"Copied:\n{text}" if as_is else f"Copied: {text}", widget,
                      QRect(), COPIED_MS)


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

    def enterEvent(self, e):
        text = self.shown_text().strip()
        self.button.setVisible(self.copyable and bool(text) and text != NA)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.button.hide()
        super().leaveEvent(e)


class CopyLineEdit(QLineEdit):
    """A typed-over value. Double-click, Ctrl+C with nothing selected, or "Copy value" in the
    right-click menu copy the value shown: the one typed, or else the automatic one."""
    def shown(self):
        return self.text() or self.placeholderText()

    def mouseDoubleClickEvent(self, e):
        copy_value(self.shown(), self)
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy) and not self.hasSelectedText():
            copy_value(self.shown(), self)
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
        from schwab.auth import client_from_token_file
        if self._client is None:
            self._client = client_from_token_file(str(self.token_path), key, secret)
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
        """The last 3 digits of the account orders go to, as chosen on the Schwab tab ("" if none)."""
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
    price, error or ""). side is "BUY" or "SELL"."""
    placed = pyqtSignal(str, str, object, str)
    checked = pyqtSignal(str, str, object, str)
    canceled = pyqtSignal(str, str, str)
    quotes = pyqtSignal(object)
    orders = pyqtSignal(str, object, str)
    replaced = pyqtSignal(str, str, object, float, str)

    def __init__(self, session: FeeReader):
        super().__init__()
        self.session = session

    def _start(self, target, *args):
        threading.Thread(target=target, args=args, name="order", daemon=True).start()

    def place(self, key, ticker, side, shares, price, account):
        """account: the last 3 digits of the account to send it to."""
        self._start(self._place, key, ticker, side, shares, price, account)

    def check(self, key, side, order_id, account=None):
        self._start(self._check, key, side, order_id, account)

    def cancel(self, key, side, order_id, account=None):
        self._start(self._cancel, key, side, order_id, account)

    def read_quotes(self, tickers):
        self._start(self._quotes, list(tickers))

    def read_orders(self, account):
        """The account's orders of the last 60 days (Schwab's limit), as order_info()."""
        self._start(self._orders, account)

    def replace(self, key, ticker, side, order_id, shares, price, account, duration=None, session=None):
        """Changes an open limit order's price, as thinkorswim does: Schwab replaces it with a new
        order (the old one becomes REPLACED). duration / session: the old order's (GTC, NORMAL
        when not given)."""
        self._start(self._replace, key, ticker, side, order_id, shares, price, account, duration, session)

    @staticmethod
    def _limit_order(ticker, side, shares, price, duration=None, session=None):
        from schwab.orders.common import Duration, Session
        from schwab.orders.equities import equity_buy_limit, equity_sell_limit
        make = equity_buy_limit if side == "BUY" else equity_sell_limit
        try:
            duration = Duration(duration) if duration else Duration.GOOD_TILL_CANCEL
        except ValueError:
            duration = Duration.GOOD_TILL_CANCEL
        try:
            session = Session(session) if session else Session.NORMAL
        except ValueError:
            session = Session.NORMAL
        return make(ticker, int(shares), limit_text(price)).set_duration(duration).set_session(session).build()

    def _place(self, key, ticker, side, shares, price, account_tail):
        try:
            from schwab.utils import Utils
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                order = self._limit_order(ticker, side, shares, price)
                r = client.place_order(account, order)
                if r.status_code not in (200, 201):
                    self.placed.emit(key, side, None, f"Schwab refused the order: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    order_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    order_id = None
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

    def _replace(self, key, ticker, side, order_id, shares, price, account_tail, duration, session):
        try:
            from schwab.utils import Utils
            with self.session._lock:
                client, account = self.session.connect(account_tail)
                order = self._limit_order(ticker, side, shares, price, duration, session)
                r = client.replace_order(account, order_id, order)
                if r.status_code not in (200, 201):
                    self.replaced.emit(key, side, None, price,
                                       f"Schwab refused the change: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    new_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    new_id = None       # found among the account's orders at the next check
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


OPEN_GONE = {"CANCELED", "REPLACED"}     # an open order that may have been changed in thinkorswim


def order_info(o):
    """A Schwab order, short: {"id", "ticker", "side" ("BUY" or "SELL"), "shares" (left to fill),
    "quantity", "filled", "price", "status", "open", "entered", "duration", "session", "type"};
    None for an order that isn't a one-leg stock order."""
    try:
        legs = o.get("orderLegCollection") or []
        if len(legs) != 1:
            return None
        leg = legs[0]
        instruction = leg.get("instruction", "")
        side = "BUY" if instruction.startswith("BUY") else "SELL" if instruction.startswith("SELL") else None
        if side is None:
            return None
        quantity = int(o.get("quantity") or leg.get("quantity") or 0)
        filled = int(o.get("filledQuantity") or 0)
        status = o.get("status", "")
        return {"id": str(o.get("orderId")), "ticker": leg.get("instrument", {}).get("symbol", ""),
                "side": side, "quantity": quantity, "filled": filled,
                "shares": int(o.get("remainingQuantity") or (quantity - filled)),
                "price": o.get("price"), "status": status,
                "open": status not in ORDER_DONE and status not in ("PENDING_CANCEL", "PENDING_REPLACE"),
                "entered": o.get("enteredTime", ""), "duration": o.get("duration"),
                "session": o.get("session"), "type": o.get("orderType")}
    except (TypeError, ValueError, AttributeError):
        return None


def tint(value) -> str:
    """The style attribute for a dialog value given as (text, colour); nothing for plain text."""
    return f" style='color:{value[1]}'" if isinstance(value, tuple) else ""


def limit_text(price) -> str:
    """A limit price as Schwab takes it: 0.0395, 1.25."""
    return f"{price:.6f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.2f}"


def usd_signed(v) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def fee_rows(fee, total, sell=False):
    """The fee rows for an order dialog: Schwab's fee, if read, and the total with it (a buy pays
    it on top, a sell gets the total less it)."""
    if fee is None:
        return [("Fee", f"{NA} (not read from Schwab yet)")]
    if fee > FEE_HIGH:      # in the section titles' yellow, as on the panel
        shown = (f"${fee:.2f} (more than ${FEE_HIGH:.2f}!)", "#e3b23c")
    else:
        shown = f"${fee:.2f}"
    if sell:
        return [("Fee (from Schwab)", shown), ("You get, less the fee", f"${total - fee:,.2f}")]
    return [("Fee (from Schwab)", shown), ("Total with the fee", f"${total + fee:,.2f}")]


def confirm_order(parent, account_name, title, rows, action, color=BUY_COLOR):
    """Asks before an order goes out. rows: [(label, value)], a value can be (text, its colour).
    account_name: the account it goes to, shown first for checking (it is chosen on the Schwab
    tab). color: the send button's. True when approved."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(f"<b>{title}</b>"))
    form = QGridLayout()
    form.setHorizontalSpacing(14)
    rows = [("Account", (account_name, ORDER_COLOR))] + rows
    for i, (k, v) in enumerate(rows):
        form.addWidget(QLabel(f"<span style='color:#a8a8a8'>{k}</span>"), i, 0)
        form.addWidget(QLabel(f"<b{tint(v)}>{v[0] if isinstance(v, tuple) else v}</b>"), i, 1)
    form.itemAtPosition(0, 1).widget().setToolTip("Chosen on the Schwab tab")
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
    return dlg.exec() == QDialog.DialogCode.Accepted


# ------------------------------------------------------------ ATM trades ---
class AtmTrades(QObject):
    """The ATM trades, kept in atm.json: each Buy ATM sent, with the plan and the market at that
    moment, and what happened since. state: "buy_open" (waiting for the buy) -> "bought" (you own
    it) -> "sell_open" (waiting for the sell) -> "sold". A buy canceled (or rejected, or expired)
    before anything was bought is "canceled". They all stay until cleared on the Schwab tab. The open orders are checked
    at Schwab every ORDER_CHECK_S seconds. Also keeps the latest market of the trades' tickers."""
    changed = pyqtSignal()          # the trades, or their states
    market_changed = pyqtSignal(str)    # a ticker's bid / ask / Level II
    buy_sent = pyqtSignal(str)      # a Buy ATM went out for this ticker

    def __init__(self, path: Path, settings: dict, save_settings, fee_reader=None, trader=None):
        super().__init__()
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.fee_reader, self.trader = fee_reader, trader
        self.entries = self._read()
        self.sending = set()        # tickers with a buy on its way, entry ids with a sell on its way
        self.pending = {}           # the same keys -> what was sent
        self.canceling = set()      # entry ids whose order is being canceled
        self.market = {}            # ticker -> {"bid": [price, size], "ask": [...], "bids": [[mm, price, size]], "asks"}
        self.schwab_orders = {}     # account tail -> its orders of the last 60 days, as order_info()
        self.orders_time = {}       # account tail -> when they were read (time.time())
        self.cancel_ids = {}        # key -> the order id the app is canceling (not a thinkorswim change)
        self.app_canceled = set()   # order ids the app canceled
        self._migrate()
        if trader:
            trader.placed.connect(self._placed)
            trader.checked.connect(self._checked)
            trader.canceled.connect(self._canceled)
            trader.quotes.connect(self._quotes)
            trader.orders.connect(self._orders)
            trader.replaced.connect(self._replaced)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        self.timer.start(ORDER_CHECK_S * 1000)
        QTimer.singleShot(3000, self.check)

    # ---- the file ----
    def _read(self):
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
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

    def save(self):
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
        """An order on its way to Schwab, or being canceled."""
        return (ticker in self.sending if ticker else False) or (
            e is not None and (e["id"] in self.sending or e["id"] in self.canceling))

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
            return got - cost - 2 * fee
        if e.get("state") == "canceled" or not e.get("filled") or bid is None:
            return None
        return got + AtmTrades.remaining(e) * bid - cost - 2 * fee

    # ---- orders ----
    def send_buy(self, snapshot):
        """Sends the Buy ATM: snapshot is the entry-to-be (the plan and the market now)."""
        ticker = snapshot["ticker"]
        self.sending.add(ticker)
        self.pending[ticker] = snapshot
        self.changed.emit()
        self.trader.place(ticker, ticker, "BUY", snapshot["shares"], snapshot["entry"], snapshot["account"])

    def ask_sell(self, parent, e, price, fee=None):
        """Sells all the stocks owned at `price`, GTC, after the dialog is approved."""
        if self.trader is None or e.get("state") != "bought" or self.busy(e):
            return
        if not price:
            QMessageBox.warning(parent, "Sell ATM", "There's no exit price.")
            return
        shares = self.remaining(e)
        total = shares * price
        fee = e.get("fee") if fee is None else fee
        rows = [("Ticker", e["ticker"]), ("Order", "SELL · Limit · GTC (good till canceled)"),
                ("Amount of stocks", f"{shares:,} (all)"), ("Limit price (exit)", fmt_price(price)),
                ("Total", f"${total:,.2f}")] + fee_rows(fee, total, sell=True)
        used = FEE_USD if fee is None else fee
        gain = (e.get("proceeds") or 0) + total - (e.get("filled") or 0) * e["entry"] - 2 * used
        rows.append(("Profit after both fees", usd_signed(gain)))
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(e.get("account")), f"Sell {shares:,} {e['ticker']} at "
                             f"{fmt_price(price)}?", rows, "Send SELL order", SELL_COLOR):
            return
        self.sending.add(e["id"])
        self.pending[e["id"]] = {"sell_price": price, "shares": shares}
        self.changed.emit()
        self.trader.place(e["id"], e["ticker"], "SELL", shares, price, e.get("account"))

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

    def tracked_ids(self):
        """The order ids the ATM trades follow, now or before."""
        ids = set()
        for e in self.entries:
            for k in ("order_id", "sell_id"):
                if e.get(k):
                    ids.add(str(e[k]))
            ids.update(str(x) for x in e.get("replaced_ids", []))
        return ids

    def other_orders(self, ticker, account=None):
        """The open limit orders of the ticker in the account (the chosen one) that aren't an ATM
        trade's: sent from thinkorswim."""
        tracked = self.tracked_ids()
        return [o for o in self.schwab_orders.get(account or self.chosen_account(), [])
                if o["ticker"] == ticker and o["open"] and o["price"] and o["shares"] > 0
                and o["id"] not in tracked]

    def chart_orders(self, ticker):
        """The ticker's open orders for the chart: the ATM trade's, then the account's others.
        [{"key", "side", "shares", "price", "atm", "duration"}]."""
        out = []
        e = self.active(ticker)
        if e and e.get("state") == "buy_open" and not e.get("gone"):
            out.append({"key": f"atm:{e['id']}:BUY", "side": "BUY", "atm": True, "duration": "GTC",
                        "shares": e["shares"] - (e.get("filled") or 0), "price": e["entry"]})
        elif e and e.get("state") == "sell_open" and e.get("sell_price") and not e.get("gone"):
            out.append({"key": f"atm:{e['id']}:SELL", "side": "SELL", "atm": True, "duration": "GTC",
                        "shares": self.remaining(e) - (e.get("sell_filled") or 0), "price": e["sell_price"]})
        for o in self.other_orders(ticker):
            duration = {"GOOD_TILL_CANCEL": "GTC", "DAY": "Day"}.get(o.get("duration"), o.get("duration"))
            out.append({"key": f"schwab:{o['id']}", "side": o["side"], "atm": False, "duration": duration,
                        "shares": o["shares"], "price": o["price"]})
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
            elif side == "SELL" and e.get("state") == "sell_open":
                order_id, old, shares, done = (e.get("sell_id"), e.get("sell_price"), self.remaining(e),
                                               e.get("sell_filled") or 0)
            else:
                return
            if done:
                QMessageBox.warning(parent, "Change the order", f"{done:,} of the {shares:,} were already "
                                    f"{'bought' if side == 'BUY' else 'sold'}: change it in thinkorswim.")
                return
            ticker, account, fee, duration, session = e["ticker"], e.get("account"), e.get("fee"), None, None
            what = "an ATM trade"
        elif o is not None:
            if key in self.canceling or key in self.sending:
                return
            order_id, old, shares, ticker = o["id"], o["price"], o["shares"], o["ticker"]
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
        total = shares * price
        rows = [("Ticker", ticker), ("Order", f"{side} · Limit · {what}"), ("Amount of stocks", f"{shares:,}"),
                ("Limit price now", fmt_price(old)), ("New limit price", (fmt_price(price), ORDER_COLOR)),
                ("Total", f"${total:,.2f}")] + (fee_rows(fee, total, sell=side == "SELL") if e is not None else [])
        if self.fee_reader:
            self.fee_reader.load_accounts()
        if not confirm_order(parent, self.account_name(account), f"Change the {side.lower()} of {shares:,} {ticker} "
                             f"from {fmt_price(old)} to {fmt_price(price)}?", rows, f"Change {side} order",
                             BUY_COLOR if side == "BUY" else SELL_COLOR):
            return
        busy_key = e["id"] if e is not None else key
        self.sending.add(busy_key)
        self.pending[busy_key] = {"old_id": str(order_id), "key": key}
        self.changed.emit()
        self.trader.replace(busy_key, ticker, side, order_id, shares, price, account, duration, session)

    def _replaced(self, key, side, new_id, price, error):
        self.sending.discard(key)
        pending = self.pending.pop(key, {})
        if error:
            QMessageBox.warning(QApplication.activeWindow(), f"{side.title()} order not changed", error)
            self.changed.emit()
            return
        e = self.find(key)
        if e is not None:
            self._adopt(e, side, {"id": str(new_id) if new_id else None, "price": price},
                        pending.get("old_id"), 0, "Changed from the chart")
        self.check()            # the account's orders again: the new one, and its number if not known
        self.changed.emit()

    def _adopt(self, e, side, new, old_id, old_filled, how):
        """The ATM trade follows its order's replacement `new` (order_info(), or {"id", "price"}):
        the new order's number and price, kept in atm.json (the checklist and the Schwab tab show
        it, the chart draws it). old_filled: what the old order filled (a sell's)."""
        price = new["price"]
        if old_id:
            e.setdefault("replaced_ids", []).append(str(old_id))
        e.pop("gone", None)
        e.pop("status", None)
        if side == "BUY":
            e.setdefault("entry-original", e["entry"])
            old = e["entry"]
            e.update(entry=price, usd=round(e["shares"] * price, 2), note=f"{how}: the buy price "
                     f"{fmt_price(old)} -> {fmt_price(price)}.")
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
            if self.active(e["ticker"]) is e:     # the checklist's exit follows
                self.settings.setdefault("checklist_prices", {}).setdefault(e["ticker"], {})["exit"] = limit_text(price)
                self.save_settings(self.settings)
        self.save()
        self.update_live(e["ticker"])

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
        self.schwab_orders[account] = orders
        self.orders_time[account] = time.time()
        # trades whose order was canceled or replaced outside the app: follow the new one, or, once
        # the orders read after it show none, take it as canceled
        for e in list(self.entries):
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
        """The price Sell ATM sells at (starts as the planned exit). The checklist's exit follows."""
        if not price or price <= 0 or e.get("sell_at") == price:
            return
        e["sell_at"] = price
        self.save()
        if self.active(e["ticker"]) is e:
            self.settings.setdefault("checklist_prices", {}).setdefault(e["ticker"], {})["exit"] = limit_text(price)
            self.save_settings(self.settings)
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
        self.entries = [x for x in self.entries if x is not e]
        self.save()
        self.changed.emit()

    def check(self):
        """Asks Schwab about every open order, and for the quotes of the open trades' tickers."""
        if self.trader is None:
            return
        for e in self.entries:
            if e["id"] in self.sending:
                continue
            if e.get("state") == "buy_open" and e.get("order_id"):
                self.trader.check(e["id"], "BUY", e["order_id"], e.get("account"))
            elif e.get("state") == "sell_open" and e.get("sell_id"):
                self.trader.check(e["id"], "SELL", e["sell_id"], e.get("account"))
        tickers = self.open_tickers()
        if tickers:
            self.trader.read_quotes(tickers)
        # the accounts' orders: the chosen one's are drawn on the chart; an open trade's show a
        # change made in thinkorswim
        accounts = {self.chosen_account()} | {e.get("account") for e in self.entries
                                               if e.get("state") in ("buy_open", "sell_open")}
        for account in sorted(a for a in accounts if a):
            self.trader.read_orders(account)

    # ---- answers from Schwab ----
    def _placed(self, key, side, order_id, message):
        self.sending.discard(key)
        pending = self.pending.pop(key, {})
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
            e.update(state="sell_open", sell_price=pending.get("sell_price"), sell_id=order_id,
                     sell_time=now, sell_filled=0, note=message)
            e.pop("status", None)
        self.save()
        self.changed.emit()
        if order_id:
            QTimer.singleShot(5000, self.check)

    def _canceled(self, key, side, error):
        self.canceling.discard(key)
        order_id = self.cancel_ids.pop(key, None)
        if error:
            self.app_canceled.discard(order_id)
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
            e["filled"] = filled
            if status == "FILLED" or (status in ORDER_DONE and filled):
                e.update(state="bought", bought_time=now,
                         note="" if status == "FILLED" else
                         f"The buy order was {status.lower()} after {filled:,} of {e['shares']:,}.")
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
                    e.update(state="bought", note=f"The sell order was {status.lower()}"
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
        if e.get("state") == "sell_open" and e.get("sell_price"):
            p["exit"] = e["sell_price"]         # the sell order's price
        changed = False
        for key, value in (("exit-live", p["exit"]), ("safety-net-live", p["stop"])):
            if value is not None and e.get(key) != value:     # kept while not known (Level II off)
                e[key] = value
                changed = True
        if changed:
            self.save()
            self.changed.emit()         # the Schwab tab's Sell at follows the live exit

    # ---- the market of the trades' tickers ----
    def _known(self, ticker):
        return any(e.get("ticker") == ticker for e in self.entries)

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

    def __init__(self, path: Path, settings: dict, save_settings, token_path: Path = None, parent=None):
        super().__init__(parent)
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.fees = {}          # ticker -> (fee or None, details), read from Schwab once a session
        self.fee_reader = FeeReader(settings, token_path) if token_path else None
        self.trader = SchwabTrader(self.fee_reader) if self.fee_reader else None
        # The ATM trades (atm.json), shared with the Schwab tab and the chart
        self.atm = AtmTrades(path.parent / "atm.json", settings, save_settings, self.fee_reader, self.trader)
        self.atm.changed.connect(self.refresh)
        self.price_tools = {}   # price key -> (its +/- buttons, its reset button)
        if self.fee_reader:
            self.fee_reader.done.connect(self.fee_read)
        self.setObjectName("checklist")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.ticker = ""
        self.rows, self.market_days, self.range, self.period = [], [], None, 50
        self.bid = self.ask = None
        self.bid_size = self.ask_size = None
        self.bids, self.asks, self.l2_live = [], [], False
        self.widgets = []       # (item, mark label, text label, value label / line edit or None)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("cl_title")
        head.addWidget(self.title, 1)
        reload_btn = QToolButton()
        reload_btn.setText("⟳")
        reload_btn.setToolTip("Reload checklist.txt after editing it, read the fee from Schwab again and check the open orders")
        reload_btn.clicked.connect(self.reload)
        head.addWidget(reload_btn)
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
                                 "The trade stays on the Schwab tab until you clear it there.")
        self.done_btn.clicked.connect(self.reset_done)
        self.done_btn.hide()
        for b in (self.buy_btn, self.sell_btn, self.done_btn):
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
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # No scroll bar (it took room on the left and was hard to see); the mouse wheel still scrolls
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)
        self.load()

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
        lay.setSpacing(3)
        self.widgets = []
        self.price_tools = {}
        self.locked = {}        # key -> its fixed cell (the plan sent with Buy ATM), shown once sent
        self.boxes = {}         # key -> its live value's box
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
                if item.kind == "info" and item.key not in TARGETS:
                    mark.setFixedWidth(4)
                if item.kind == "info" and item.key in EDITABLE:
                    value = self.make_edit(item.key)
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
                        value.setCursor(Qt.CursorShape.PointingHandCursor)
                        value.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                        value.mousePressEvent = lambda e, b=box: b.copy() if e.button() == Qt.MouseButton.LeftButton else None
                    else:
                        box = CopyBox(value, shown)
                    self.boxes[item.key] = box
                    if item.key in LOCKED:
                        # the fixed cell, the box, +/- and reset close together, left to right
                        group = QWidget()
                        group.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                        glay = QHBoxLayout(group)
                        glay.setContentsMargins(0, 0, 0, 0)
                        glay.setSpacing(2)
                        lock = QLabel()
                        lock.setObjectName("cl_locked")
                        lock.setFixedWidth(LOCKED_W)       # the live cells beside them in one column
                        lock.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                        lock.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                        lock.hide()
                        lock.setFixedHeight(value.sizeHint().height() if isinstance(value, QLineEdit) else 22)
                        self.locked[item.key] = lock
                        glay.addWidget(lock, 0, Qt.AlignmentFlag.AlignVCenter)
                        glay.addWidget(box)
                        if item.key in EDITABLE and item.key != "size":
                            stepper, reset = self.make_stepper(item.key, value), self.make_reset(item.key, value)
                            self.price_tools[item.key] = (stepper, reset)
                            glay.addWidget(stepper)
                            glay.addWidget(reset)
                        box = group
                    row.addWidget(box)
                lay.addLayout(row)
                self.widgets.append((item, mark, text, value))
        lay.addStretch()
        self.scroll.setWidget(body)
        self.refresh()

    def make_edit(self, key):
        edit = CopyLineEdit()
        edit.setFixedWidth(84)
        edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        edit.setValidator(QRegularExpressionValidator(QRegularExpression(
            r"\d{0,7}" if key == "size" else r"\d{0,6}(\.\d{0,6})?"), edit))
        pal = edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor("#f0a020"))              # a typed-over value
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#ffffff"))    # the automatic value
        edit.setPalette(pal)
        edit.textEdited.connect(lambda text, k=key: self.typed(k, text))
        edit.editingFinished.connect(lambda k=key, e=edit: self.edit_done(k, e))
        return edit

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
            b.setFixedSize(18, 12)
            b.setToolTip(f"{'Up' if step > 0 else 'Down'} 0.0001")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c=False, s=step: self.step(key, edit, s))
            lay.addWidget(b)
        return box

    def make_reset(self, key, edit):
        """A round arrow that drops the typed price, back to the automatic one."""
        b = QToolButton()
        b.setObjectName("cl_reset")
        b.setIcon(reset_icon())
        b.setFixedSize(18, 25)
        b.setToolTip("Reset to the automatic value")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda: self.reset_value(key, edit))
        return b

    def reset_value(self, key, edit):
        if not edit.isEnabled():
            return
        edit.clear()
        self.typed(key, "")
        self.save_settings(self.settings)

    def step(self, key, edit, direction):
        """Moves the price shown (typed, or else the automatic one) one 0.0001 step."""
        if not edit.isEnabled():
            return
        try:
            base = Decimal(edit.shown().replace(",", "").replace("$", "").strip())
        except InvalidOperation:
            return
        new = base + direction * PRICE_STEP
        if new <= 0:
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
        text = text.strip()
        try:
            valid = float(text) > 0
        except ValueError:
            valid = False
        if key == "size":
            if valid:
                self.settings["checklist_size"] = text
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
            if valid and key == "exit" and order and order.get("state") in ("buy_open", "bought"):
                self.atm.set_sell_at(order, float(text))    # Sell ATM sells at it
            if key in ("exit", "stop"):
                self.atm.update_live(self.ticker)
        self.refresh()

    def edit_done(self, key=None, edit=None):
        """Enter or leaving the box. A price typed as 1 or more is a count of 0.0001 steps:
        12 -> 0.0012, 1 -> 0.0001 (these stocks trade under $1)."""
        if edit is not None and key != "size":
            try:
                typed = Decimal(edit.text().strip())
            except InvalidOperation:
                typed = None
            if typed is not None and typed >= 1:
                text = format(typed * PRICE_STEP, "f")
                edit.setText(text)
                self.typed(key, text)
        self.save_settings(self.settings)
        self.refresh()

    # ---- the ATM trade of the ticker shown (atm.json, through self.atm) ----
    def order(self, ticker=None):
        return self.atm.active(ticker or self.ticker)

    @staticmethod
    def style_button(button, name, text):
        button.setText(text)
        button.setIcon(red_x_icon() if name == "cl_cancel" else QIcon())   # Cancel Buy / Cancel Sell
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
        else:
            self.style_button(self.buy_btn, "cl_buy", "Buy ATM")
            self.buy_btn.setEnabled(can and state is None)
        if state == "sell_open":
            self.style_button(self.sell_btn, "cl_cancel", "Cancel Sell")
            self.sell_btn.setEnabled(can)
        else:
            self.style_button(self.sell_btn, "cl_sell", "Sell ATM")
            self.sell_btn.setEnabled(can and state == "bought")
        self.done_btn.setVisible(state == "sold")
        if self.trader is None:
            self.buy_btn.setToolTip("Needs the Schwab login")
            self.sell_btn.setToolTip("Needs the Schwab login")
        else:
            self.buy_btn.setToolTip(
                "Cancel the buy order (after you approve it)" if state == "buy_open" else
                "Send a GTC limit buy at the entry price for the amount of stocks, after you approve it"
                if state is None else "There is an ATM trade for this ticker (Schwab tab)")
            self.sell_btn.setToolTip(
                "Cancel the sell order, to change its price or keep the stocks longer" if state == "sell_open" else
                "Sell all the stocks at the exit price, GTC, after you approve it" if state == "bought" else
                "Once the stocks are bought")
        text = ""
        if self.ticker in self.atm.sending or (e and e["id"] in self.atm.sending):
            text = "Sending the order to Schwab..."
        elif e and e["id"] in self.atm.canceling:
            text = "Canceling the order..."
        elif e:
            status = e.get("status", "")
            status = f" ({status})" if status else ""
            left = self.atm.remaining(e)
            if state == "buy_open":
                text = (f"Buy order open{status}: {e['shares']:,} @ {fmt_price(e['entry'])} GTC"
                        + (f" · {e['filled']:,} bought so far" if e.get("filled") else ""))
            elif state == "bought":
                text = f"You own {left:,} @ {fmt_price(e['entry'])}. Sell ATM sells them all at the exit price."
            elif state == "sell_open":
                text = (f"Sell order open{status}: {left:,} @ {fmt_price(e['sell_price'])} GTC"
                        + (f" · {e['sell_filled']:,} sold so far" if e.get("sell_filled") else ""))
            elif state == "sold":
                text = f"Sold {e.get('filled', 0):,}: {usd_signed(self.atm.profit(e))} after both fees."
            if e.get("note") and state != "sold":
                text += f"\n{e['note']}"
        self.order_label.setText(text)
        self.order_label.setVisible(bool(text))

    def buy_clicked(self):
        e = self.order()
        if e and e.get("state") == "buy_open":
            self.atm.ask_cancel(self, e, "BUY")
        else:
            self.buy()

    def sell_clicked(self):
        e = self.order()
        if e and e.get("state") == "sell_open":
            self.atm.ask_cancel(self, e, "SELL")
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
                ("Amount of stocks", f"{p['shares']:,}"), ("Limit price (entry)", fmt_price(p["entry"])),
                ("Total", f"${total:,.2f}")] + fee_rows(fee, total)
        self.fee_reader.load_accounts()
        account = self.fee_reader.order_tail()
        if not account:
            QMessageBox.warning(self, "Buy ATM", "Choose the account for orders on the Schwab tab first."
                                if self.fee_reader.accounts else "Can't read the Schwab accounts. Check the Schwab login.")
            return
        if not confirm_order(self, self.atm.account_name(account),
                             f"Buy {p['shares']:,} {ticker} at {fmt_price(p['entry'])}?", rows, "Send BUY order"):
            return
        self.atm.send_buy(self.atm_snapshot(p, account))

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

    # ---- the fee from Schwab, once per ticker a session ----
    def read_fee(self):
        if self.fee_reader and self.ticker and self.ticker not in self.fees:
            self.fees[self.ticker] = (None, "Reading the fee from Schwab...")
            self.fee_reader.request(self.ticker)

    def fee_read(self, ticker, fee, details):
        self.fees[ticker] = (fee, details)
        if ticker == self.ticker:
            self.refresh()

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
        usd = own.get("size", POSITION_USD)
        fee, fee_details = self.fees.get(self.ticker, (None, "No ticker"))
        fee_used = FEE_USD if fee is None else fee
        order = self.order()
        shares = ((self.atm.remaining(order) if order.get("state") in ("bought", "sell_open") else 0)
                  or order.get("filled") or order.get("shares")) if order else None
        if order:
            own["entry"] = order["entry"]       # the buy's price: exit and stop are worked out from it
        p = plan(bid, ask, self.bids, usd, own.get("entry"), own.get("exit"), own.get("stop"), fee_used, shares)
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
            "size": (INFO, f"${POSITION_USD:,}", f"Default ${POSITION_USD:,}, kept for all tickers. " + typed),
            "shares": (INFO, f"{p['shares']:,}" if p["shares"] else NA,
                       "From the ATM trade (Schwab tab)" if order else f"round({usd:g} / entry)"),
            "exit": (INFO, price(auto["exit"]), "Trips: the Ask. Dubs and up: the Ask - 0.0001. " + typed),
            "order": (INFO, "GTC", ""),
            "stop": (INFO, price(auto["stop"]),
                     f"The highest bid where the bids add up to {COVER_X}x our amount of stocks. " + typed),
            "profit": (INFO, pct(p["profit"]), "(exit - entry) / entry"),
            "loss": (INFO, pct(-p["loss"]) if p["loss"] is not None else NA, "(entry - stop) / entry"),
            "fee": ((UNKNOWN, NA, f"Not read from Schwab, ${FEE_USD:.2f} is used: {fee_details}")
                    if fee is None else
                    (OK if fee <= FEE_USD + 0.001 else BAD, f"${fee:.2f}",
                     fee_details + ("" if fee <= FEE_USD + 0.001 else f" More than ${FEE_USD:.2f}!"))),
            "profit_usd": (INFO, usd_text(p["profit_usd"]),
                           f"amount of stocks x (exit - entry) - 2 x ${fee_used:.2f} fees (buy and sell)"),
            "loss_usd": (INFO, usd_text(p["loss_usd"]),
                         f"-amount of stocks x (entry - stop) - 2 x ${fee_used:.2f} fees (buy and sell)"),
            "ratio": (INFO, f"1:{p['ratio']:.2f}" if p["ratio"] is not None else NA, "1 : profit % / loss % (the fees are left out)"),
        }

        # The bounce range
        if not self.rows:
            r["range"] = (UNKNOWN, "", "No candles")
        elif self.range is None:
            r["range"] = (BAD, "", "No bounce range now (ATM tab's Min gap and Touch within)")
        else:
            age = (date.fromisoformat(self.range.last_date) - date.fromisoformat(self.range.since)).days
            r["range"] = (OK if age >= RANGE_DAYS else BAD, "",
                          f"{fmt_price(self.range.low)} - {fmt_price(self.range.high)} since {self.range.since} "
                          f"({age} days, {self.range.legs} legs)")

        # Level II
        if not self.l2_live:
            for key in ("l2_live", "no_diluter", "buyers", "cushion"):
                r[key] = (UNKNOWN, "", "No Level II for this ticker yet")
            r["l2_live"] = (BAD, "", "No Level II for this ticker yet")
        else:
            r["l2_live"] = (OK, "", "")
            # Who is first on the Ask: the market makers at the best (lowest) ask price
            best = min((price for _, price, *_ in self.asks), default=None)
            first = sorted({mm for mm, price, *_ in self.asks if price == best})
            red = [mm for mm in first if mm in MM_RED]
            yellow = [mm for mm in first if mm in MM_YELLOW]
            at = f" first on the Ask ({fmt_price(best)})" if best is not None else ""
            if red:
                r["no_diluter"] = (BAD, "", "A diluter" + at + ": " + ", ".join(red))
            elif yellow:
                r["no_diluter"] = (MAYBE, "", "A so-so market maker" + at + ": " + ", ".join(yellow)
                                   + ". Could be good or bad")
            elif first:
                r["no_diluter"] = (OK, "", "First on the Ask: " + ", ".join(first))
            else:
                r["no_diluter"] = (UNKNOWN, "", "Nobody on the Ask")
            bid_total = sum(s for _, _, s, _ in self.bids)
            ask_total = sum(s for _, _, s, _ in self.asks)
            trips = is_trips(bid or ask or 1)
            good = bid_total >= ask_total if trips else ask_total <= 2 * bid_total
            r["buyers"] = (OK if good else BAD, "",
                           f"Bids {bid_total:,} · Asks {ask_total:,}"
                           + (" (Trips: Bids must be at least the Asks)" if trips else " (Asks up to 2x the Bids)"))
            if not p["shares"]:
                r["cushion"] = (UNKNOWN, "", "No entry price")
            elif auto["stop"] is None:
                r["cushion"] = (BAD, "", f"All the bids add up to {bid_total:,}, "
                                         f"less than {COVER_X}x {p['shares']:,}")
            else:
                r["cushion"] = (OK, "", f"The bids reach {COVER_X}x at {fmt_price(auto['stop'])}")

        # Volume
        stats = volume_stats(self.rows, self.market_days, self.period)
        if stats is None:
            for key in ("avg_volume", "no_spike", "steady"):
                r[key] = (UNKNOWN, "", "No candles")
            r["avg_volume"] = (UNKNOWN, NA, "No candles")
        else:
            med, spikes, dead = stats
            about = f"MedianVolume ({self.period}), as on the chart (its Averaging days)"
            if not p["shares"]:
                r["avg_volume"] = (UNKNOWN, fmt_volume(med), about + ". No amount of stocks to compare it with yet")
            else:
                need = VOL_PART * p["shares"]
                r["avg_volume"] = (OK if med >= need else BAD, fmt_volume(med),
                                   f"{about}: {med:,.0f}. It must be at least {VOL_PART:g} x our "
                                   f"{p['shares']:,} stocks = {need:,.0f}")
            r["no_spike"] = ((BAD, "", f"{SPIKE_X:g}x the median volume or more on " + ", ".join(spikes)
                              + ". Check it's not dilution.") if spikes else (OK, "", ""))
            r["steady"] = (OK if dead <= MAX_DEAD else BAD, "",
                           f"{dead} of {len(self.market_days)} market days without trades (up to {MAX_DEAD} is fine)")
        spike_bad = r["no_spike"][0] == BAD
        red_bad = r["no_diluter"][0] == BAD
        if spike_bad or red_bad:
            r["no_dilution"] = (BAD, "", " · ".join(x[2] for x, bad in ((r["no_spike"], spike_bad),
                                                                        (r["no_diluter"], red_bad)) if bad))
        elif UNKNOWN in (r["no_spike"][0], r["no_diluter"][0]):
            r["no_dilution"] = (UNKNOWN, "", "Needs the candles and the Level II")
        else:
            r["no_dilution"] = (OK, "", "")
        return r

    def plan_text(self):
        """The trade plan, as text to paste: the values the checklist shows now."""
        if not self.ticker:
            return ""
        self.evaluate()
        p, usd = self.last_plan
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
        """The fixed cells' texts: the plan sent with Buy ATM, and what it gives."""
        fee = FEE_USD if e.get("fee") is None else e["fee"]
        size = e.get("size") or e.get("usd")
        p = plan(None, None, [], size, e["entry"], e.get("exit"), e.get("safety-net"), fee, e.get("shares"))
        price = lambda v: NA if v is None else fmt_price(v)
        pct = lambda v: NA if v is None else f"{v:+.1f}%"
        usd = lambda v: NA if v is None else usd_signed(v)
        return {"size": NA if size is None else f"${size:,.2f}".replace(".00", ""),
                "entry": price(p["entry"]), "exit": price(p["exit"]), "stop": price(p["stop"]),
                "profit": pct(p["profit"]), "loss": pct(-p["loss"]) if p["loss"] is not None else NA,
                "profit_usd": usd(p["profit_usd"]), "loss_usd": usd(p["loss_usd"]),
                "ratio": f"1:{p['ratio']:.2f}" if p["ratio"] is not None else NA}

    def refresh(self):
        if not self.widgets:
            return
        results = self.evaluate()
        order = self.order()
        self.update_trade_buttons()
        fixed = self.locked_texts(order) if order else {}
        for key, lock in self.locked.items():
            lock.setVisible(bool(order))
            if order:
                lock.setText(fixed.get(key, NA))
                lock.setToolTip("Fixed: the plan sent with Buy ATM" + (
                    "" if key in ("entry", "size") else ". Beside it: now, from the live prices."))
        for key in LOCKED + ("shares", "fee", "ticker"):     # no copy buttons by the live cells once sent
            if isinstance(self.boxes.get(key), CopyBox):
                self.boxes[key].set_copyable(not order)
        # ...and the values without a fixed cell start where the fixed cells' values do
        for item, _, _, value in self.widgets:
            if item.key in ("ticker", "shares", "fee") and isinstance(value, QLabel):
                value.setIndent(LOCKED_INDENT if order else -1)
            elif item.key in LOCKED and isinstance(value, QLabel) and not isinstance(value, QLineEdit):
                value.setIndent(LIVE_INDENT if order else -1)     # in line with the live price boxes
        for key in ("entry", "size"):       # only the fixed cell once sent
            if key in self.boxes:
                self.boxes[key].setVisible(not order)
            for w in self.price_tools.get(key, ()):
                w.setVisible(not order)
        ticks = self.ticked()
        manual, solvers = {}, {}      # the manual items' marks and texts, by their keys
        for item, *_ in self.widgets:
            if item.kind == "manual":
                manual[item.key] = ticks.get(item.key, ticks.get(item.text, EMPTY))
                solvers[item.key] = item.text
        passed = total = 0
        for item, mark, text, value in self.widgets:
            if item.kind == "manual":
                state, val, tip = ticks.get(item.key, ticks.get(item.text, EMPTY)), "", "Click: empty → ✔ → ✘ → empty"
            else:
                state, val, tip = results.get(item.key, (UNKNOWN, "", f"No rule called \"{item.key}\""))
                if state == BAD and item.solved_by and manual.get(item.solved_by) == OK:
                    state = SOLVED
                    tip = (tip + " · " if tip else "") + f"Solved: \"{solvers.get(item.solved_by, item.solved_by)}\" is checked"
            if isinstance(value, QLineEdit):
                value.setPlaceholderText(val)
                state = order.get("state") if order else None
                # the exit while the sell order works (Cancel Sell to change it) or once sold
                locked = item.key == "exit" and state in ("sell_open", "sold")
                if locked:
                    tip = "The sell order's price: Cancel Sell to change it."
                elif order and item.key == "exit":
                    tip += " Sell ATM sells at it."
                value.setToolTip(tip)
                value.setReadOnly(locked)
                tools = self.price_tools.get(item.key)
                if tools:
                    tools[0].setEnabled(not locked)
                    tools[1].setEnabled(not locked)
                value.setEnabled(bool(self.ticker) or item.key == "size")
                own = self.override_text(item.key)
                if not value.hasFocus() and value.text() != own:
                    value.setText(own)
                continue
            if item.kind == "info":
                value.setText(val)
                value.setToolTip(tip)
                if item.key == "shares":
                    value.setStyleSheet(f"color: {ORDER_COLOR};" if order else "")
                if item.key in ("profit", "loss", "profit_usd", "loss_usd"):
                    value.setStyleSheet("" if val in ("", NA) else
                                        f"color: {MARKS[OK if val.startswith('+') else BAD][1]};")
                if item.key not in TARGETS:
                    continue
                # ✔ once it reaches its target, else ✘ (? while unknown)
                number = self.last_plan[0][item.key]
                need = TARGETS[item.key]
                state = UNKNOWN if number is None else OK if number >= need - 1e-9 else BAD
                tip = (f"{tip} · " if tip else "") + (f"At least {need:g}%" if item.key == "profit" else
                                                      f"At least 1:{need:g}")
                symbol, color = MARKS[state]
                mark.setText(f"<b style='color:{color}'>{symbol}</b>")
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
            symbol, color = MARKS[state]
            mark.setText(f"<b style='color:{color}'>{symbol}</b>")
            for w in (mark, text):
                w.setToolTip(tip)
            total += 1
            passed += state in (OK, SOLVED)
        self.summary.setText(f"{self.ticker} · {passed}/{total} ✔" if self.ticker else "")
        self.score = (passed, total)        # Buy ATM asks first when not all are ✔
