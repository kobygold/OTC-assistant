"""The trade checklist next to the chart: the items of checklist.txt in Hebrew, each with a green
check when its condition is met and a red X when it isn't. "auto" items are worked out from the
chart's candles and the live Level II, "manual" ones are clicked by hand (saved per ticker): empty,
then a check, then an X, then empty again, and
"info" ones show a value: the entry, exit and stop prices, the amount of stocks and so on.
The position size and the entry, exit and stop prices can be typed over: the position size is kept
for all tickers, the prices per ticker, and clearing a field brings back the automatic value."""

import json
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from PyQt6.QtCore import QObject, QPointF, QTimer, QRect, QRectF, QRegularExpression, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QPolygonF,
                         QRegularExpressionValidator)
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QToolButton, QToolTip,
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
PRICE_STEP = Decimal("0.0001")      # the +/- buttons; a typed whole number means that many steps (12 -> 0.0012)
ORDER_COLOR = "#4fc3f7"     # the plan's values once a BUY order was sent: fixed, not the live ones
ORDER_CHECK_S = 60          # how often open orders are checked at Schwab, in seconds
# Schwab order statuses after which an order no longer works
ORDER_DONE = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}

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
#checklist QPushButton#cl_buy { background: #1c7a3a; }
#checklist QPushButton#cl_buy:hover { background: #239447; }
#checklist QPushButton#cl_sell { background: #a0303b; }
#checklist QPushButton#cl_sell:hover { background: #bb3946; }
#checklist QPushButton#cl_buy:disabled, #checklist QPushButton#cl_sell:disabled {
    background: #1d2125; color: #5c6268; border-color: #2c3136; }
#checklist QLabel#cl_order { color: #4fc3f7; }
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


class CopyBox(QWidget):
    """A value with a small copy button on its left (the line's left end), shown while the mouse is over them."""
    def __init__(self, value, shown_text, parent=None, copy_text=None):
        """shown_text: the value shown (the button shows only when there is one). copy_text: what
        to copy instead of it, exactly as given."""
        super().__init__(parent)
        self.shown_text = shown_text
        self.copy_text = copy_text
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

    def enterEvent(self, e):
        text = self.shown_text().strip()
        self.button.setVisible(bool(text) and text != NA)
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

    def __init__(self, settings: dict, token_path: Path):
        super().__init__()
        self.settings, self.token_path = settings, token_path
        self._lock = threading.Lock()       # one Schwab call at a time
        self._client = self._account = None
        self.account_tail = ""          # the account number's last 3 digits, once logged in

    def request(self, ticker):
        threading.Thread(target=self._read, args=(ticker,), name="fee", daemon=True).start()

    def _read(self, ticker):
        try:
            with self._lock:
                fee, details = self._preview(ticker)
            self.done.emit(ticker, fee, details)
        except Exception as e:      # never let the thread die silently
            self.done.emit(ticker, None, f"{type(e).__name__}: {e}")

    def connect(self):
        """Logs in once (call it holding self._lock): the client and the first account's hash.
        Raises RuntimeError with the reason when it can't."""
        key = self.settings.get("schwab_app_key", "").strip()
        secret = self.settings.get("schwab_app_secret", "").strip()
        if not (key and secret and self.token_path.exists()):
            raise RuntimeError("No Schwab login (schwab_app_key, schwab_app_secret and the Level II login)")
        from schwab.auth import client_from_token_file
        if self._client is None:
            self._client = client_from_token_file(str(self.token_path), key, secret)
        if self._account is None:
            r = self._client.get_account_numbers()
            if r.status_code != 200:
                raise RuntimeError(f"Schwab accounts: HTTP {r.status_code}")
            first = r.json()[0]
            self._account = first["hashValue"]
            self.account_tail = str(first.get("accountNumber", ""))[-3:]    # shown as ...123, never in full
        return self._client, self._account

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
        r = self._client.preview_order(self._account, order)
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
    """Sends the checklist's GTC limit orders to Schwab and reads their status, in background
    threads, through the FeeReader's login (one client, one call at a time).
    placed(ticker, side, order id or None, message) and checked(ticker, side, order or None,
    message) arrive in the GUI thread. side is "BUY" or "SELL"."""
    placed = pyqtSignal(str, str, object, str)
    checked = pyqtSignal(str, str, object, str)

    def __init__(self, session: FeeReader):
        super().__init__()
        self.session = session

    def place(self, ticker, side, shares, price, replace_id=None):
        threading.Thread(target=self._place, args=(ticker, side, shares, price, replace_id),
                         name="order", daemon=True).start()

    def check(self, ticker, side, order_id):
        threading.Thread(target=self._check, args=(ticker, side, order_id), name="order status",
                         daemon=True).start()

    def _place(self, ticker, side, shares, price, replace_id):
        try:
            from schwab.orders.common import Duration, Session
            from schwab.orders.equities import equity_buy_limit, equity_sell_limit
            from schwab.utils import Utils
            with self.session._lock:
                client, account = self.session.connect()
                make = equity_buy_limit if side == "BUY" else equity_sell_limit
                order = (make(ticker, int(shares), limit_text(price))
                         .set_duration(Duration.GOOD_TILL_CANCEL).set_session(Session.NORMAL).build())
                if replace_id:
                    r = client.replace_order(account, replace_id, order)
                else:
                    r = client.place_order(account, order)
                if r.status_code not in (200, 201):
                    self.placed.emit(ticker, side, None, f"Schwab refused the order: HTTP {r.status_code} {r.text[:300]}")
                    return
                try:
                    order_id = Utils(client, account).extract_order_id(r)
                except ValueError:
                    order_id = None
            self.placed.emit(ticker, side, order_id,
                             "" if order_id else "Schwab took the order but didn't say its number. "
                                                 "Check it in Schwab; the app can't follow it.")
        except Exception as e:      # never let the thread die silently
            self.placed.emit(ticker, side, None, f"{type(e).__name__}: {e}")

    def _check(self, ticker, side, order_id):
        try:
            with self.session._lock:
                client, account = self.session.connect()
                r = client.get_order(order_id, account)
            if r.status_code != 200:
                self.checked.emit(ticker, side, None, f"HTTP {r.status_code}")
                return
            self.checked.emit(ticker, side, r.json(), "")
        except Exception as e:
            self.checked.emit(ticker, side, None, f"{type(e).__name__}: {e}")


def limit_text(price) -> str:
    """A limit price as Schwab takes it: 0.0395, 1.25."""
    return f"{price:.6f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.2f}"


# ---------------------------------------------------------------- the panel ---
class ChecklistPanel(QWidget):
    ticks_changed = pyqtSignal()
    order_placed = pyqtSignal(str)      # a BUY order was sent for this ticker

    def __init__(self, path: Path, settings: dict, save_settings, token_path: Path = None, parent=None):
        super().__init__(parent)
        self.path, self.settings, self.save_settings = path, settings, save_settings
        self.fees = {}          # ticker -> (fee or None, details), read from Schwab once a session
        self.fee_reader = FeeReader(settings, token_path) if token_path else None
        self.trader = SchwabTrader(self.fee_reader) if self.fee_reader else None
        self.sending = set()    # tickers with an order on its way to Schwab
        self.pending = {}       # ticker -> the order's details while it's on its way
        self.price_tools = {}   # price key -> (its +/- buttons, its reset button)
        if self.fee_reader:
            self.fee_reader.done.connect(self.fee_read)
        if self.trader:
            self.trader.placed.connect(self.order_sent)
            self.trader.checked.connect(self.order_checked)
        self.setObjectName("checklist")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.ticker = ""
        self.rows, self.market_days, self.range, self.period = [], [], None, 50
        self.bid = self.ask = None
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
        self.buy_btn = QPushButton("BUY")
        self.buy_btn.setObjectName("cl_buy")
        self.buy_btn.clicked.connect(self.buy)
        self.sell_btn = QPushButton("SELL")
        self.sell_btn.setObjectName("cl_sell")
        self.sell_btn.clicked.connect(self.sell)
        for b in (self.buy_btn, self.sell_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
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
        # Open orders are checked at Schwab now and then, for every ticker, shown or not
        self.order_timer = QTimer(self)
        self.order_timer.timeout.connect(self.check_orders)
        self.order_timer.start(ORDER_CHECK_S * 1000)
        QTimer.singleShot(3000, self.check_orders)
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
        self.check_orders()

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
                if item.kind == "info":
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
                    if item.key in EDITABLE and item.key != "size":
                        # box, +/- and reset close together, left to right
                        group = QWidget()
                        group.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                        glay = QHBoxLayout(group)
                        glay.setContentsMargins(0, 0, 0, 0)
                        glay.setSpacing(2)
                        glay.addWidget(box)
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

    # ---- orders: settings["orders"][ticker] = {state, shares, entry, exit, stop, buy_id, ...} ----
    # state: "buy_open" (sent, not filled yet) -> "bought" -> "sell_open" -> done (moved to order_history)
    def order(self, ticker=None):
        return self.settings.get("orders", {}).get(ticker or self.ticker)

    def update_trade_buttons(self):
        order = self.order()
        state = order.get("state") if order else None
        busy = self.ticker in self.sending
        can_trade = self.trader is not None
        self.buy_btn.setEnabled(bool(self.ticker) and can_trade and not busy and state is None)
        self.sell_btn.setEnabled(can_trade and not busy and state in ("bought", "sell_open"))
        self.sell_btn.setText("UPDATE SELL" if state == "sell_open" else "SELL")
        if not can_trade:
            tip = "Needs the Schwab login"
            self.buy_btn.setToolTip(tip)
            self.sell_btn.setToolTip(tip)
        else:
            self.buy_btn.setToolTip(
                "Sending..." if busy else
                "Send a GTC limit buy at the entry price for the amount of stocks, after you approve it"
                if state is None else "There is already an order for this ticker")
            self.sell_btn.setToolTip(
                "Sending..." if busy else
                "Change the open sell order's price to the exit price" if state == "sell_open" else
                "Send a GTC limit sell at the exit price, after you approve it" if state == "bought" else
                "Once the stocks are bought")
        text = ""
        if busy:
            text = "Sending the order to Schwab..."
        elif order:
            shares, filled = order.get("shares", 0), order.get("filled", 0)
            status = order.get("status", "")
            status = f" ({status})" if status else ""
            if state == "buy_open":
                text = (f"Buy order open{status}: {shares:,} @ {fmt_price(order['entry'])} GTC"
                        + (f" · {filled:,} bought so far" if filled else ""))
            elif state == "bought":
                text = f"Bought {filled or shares:,} @ {fmt_price(order['entry'])}. SELL sends them at the exit price."
            elif state == "sell_open":
                text = (f"Sell order open{status}: {filled or shares:,} @ {fmt_price(order['sell_price'])} GTC"
                        + (f" · {order.get('sold', 0):,} sold so far" if order.get("sold") else ""))
            if order.get("note"):
                text += f"\n{order['note']}"
        self.order_label.setText(text)
        self.order_label.setVisible(bool(text))

    def confirm(self, title, rows, action):
        """Asks before an order goes out. rows: [(label, value)]. True when approved."""
        account = getattr(self.fee_reader, "account_tail", "")
        rows = [("Account", f"...{account}" if account else "the first account of the Schwab login")] + rows
        html = "<table cellspacing='0' cellpadding='3'>" + "".join(
            f"<tr><td style='color:#a8a8a8'>{k}</td><td><b>{v}</b></td></tr>" for k, v in rows) + "</table>"
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<b>{title}</b>")
        box.setInformativeText(html)
        send = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)        # Enter doesn't send it by mistake
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is send

    def fee_rows(self, total, sell=False):
        """The fee rows for a dialog: Schwab's fee, if read, and the total with it (a buy pays it on
        top, a sell gets the total less it)."""
        fee = self.fees.get(self.ticker, (None, ""))[0]
        if fee is None:
            return [("Fee", f"{NA} (not read from Schwab yet)")]
        if sell:
            return [("Fee (from Schwab)", f"${fee:.2f}"), ("You get, less the fee", f"${total - fee:,.2f}")]
        return [("Fee (from Schwab)", f"${fee:.2f}"), ("Total with the fee", f"${total + fee:,.2f}")]

    def buy(self):
        ticker = self.ticker
        if not ticker or self.trader is None or self.order(ticker):
            return
        self.evaluate()
        p, _ = self.last_plan
        if not p["entry"] or not p["shares"]:
            QMessageBox.warning(self, "BUY", "There's no entry price or amount of stocks yet.")
            return
        total = p["shares"] * p["entry"]
        rows = [("Ticker", ticker), ("Order", "BUY · Limit · GTC (good till canceled)"),
                ("Amount of stocks", f"{p['shares']:,}"), ("Limit price (entry)", fmt_price(p["entry"])),
                ("Total", f"${total:,.2f}")] + self.fee_rows(total) + [
                ("Exit (sell) price", fmt_price(p["exit"]) if p["exit"] else NA),
                ("Stop", fmt_price(p["stop"]) if p["stop"] is not None else NA)]
        if not self.confirm(f"Buy {p['shares']:,} {ticker} at {fmt_price(p['entry'])}?", rows, "Send BUY order"):
            return
        self.pending[ticker] = {"shares": p["shares"], "entry": p["entry"], "exit": p["exit"], "stop": p["stop"]}
        self.sending.add(ticker)
        self.update_trade_buttons()
        self.trader.place(ticker, "BUY", p["shares"], p["entry"])

    def sell(self):
        ticker = self.ticker
        order = self.order(ticker)
        if not order or order.get("state") not in ("bought", "sell_open") or self.trader is None:
            return
        self.evaluate()
        p, _ = self.last_plan
        price = p["exit"]
        if not price:
            QMessageBox.warning(self, "SELL", "There's no exit price.")
            return
        shares = order.get("filled") or order["shares"]
        total = shares * price
        update = order["state"] == "sell_open"
        rows = [("Ticker", ticker), ("Order", "SELL · Limit · GTC (good till canceled)"),
                ("Amount of stocks", f"{shares:,}")]
        if update:
            rows.append(("Open sell order's price", fmt_price(order["sell_price"])))
        rows += [("Limit price (exit)", fmt_price(price)), ("Total", f"${total:,.2f}")] + self.fee_rows(total, sell=True)
        fee = self.fees.get(ticker, (None, ""))[0]
        fee = FEE_USD if fee is None else fee
        gain = shares * (price - order["entry"]) - 2 * fee
        rows.append(("Profit after both fees", f"{'+' if gain >= 0 else '-'}${abs(gain):,.2f}"))
        title = (f"Change the sell order of {ticker} to {fmt_price(price)}?" if update
                 else f"Sell {shares:,} {ticker} at {fmt_price(price)}?")
        if not self.confirm(title, rows, "Change SELL order" if update else "Send SELL order"):
            return
        self.pending[ticker] = {"sell_price": price}
        self.sending.add(ticker)
        self.update_trade_buttons()
        self.trader.place(ticker, "SELL", shares, price, order.get("sell_id") if update else None)

    def order_sent(self, ticker, side, order_id, message):
        self.sending.discard(ticker)
        pending = self.pending.pop(ticker, {})
        if order_id is None and not message.startswith("Schwab took"):
            QMessageBox.warning(self, f"{side} order not sent", f"{ticker}: {message}")
            self.refresh()
            return
        orders = self.settings.setdefault("orders", {})
        now = datetime.now().isoformat(timespec="seconds")
        if side == "BUY":
            orders[ticker] = dict(pending, state="buy_open", buy_id=order_id, filled=0, sent=now,
                                  note=message)
            # The plan stays as sent: typed values, so live quotes no longer move them
            mine = self.settings.setdefault("checklist_prices", {}).setdefault(ticker, {})
            for key in ("entry", "exit", "stop"):
                if pending.get(key) is not None:
                    mine[key] = limit_text(pending[key])
            self.order_placed.emit(ticker)
        else:
            order = orders.get(ticker, {})
            order.update(state="sell_open", sell_price=pending.get("sell_price"), sell_sent=now,
                         note=message)
            if order_id:
                order["sell_id"] = order_id
            order.pop("status", None)
        self.save_settings(self.settings)
        self.refresh()
        if order_id:
            QTimer.singleShot(5000, self.check_orders)

    def check_orders(self):
        """Asks Schwab about every open order, of every ticker."""
        if self.trader is None:
            return
        for ticker, order in list(self.settings.get("orders", {}).items()):
            if ticker in self.sending:
                continue
            if order.get("state") == "buy_open" and order.get("buy_id"):
                self.trader.check(ticker, "BUY", order["buy_id"])
            elif order.get("state") == "sell_open" and order.get("sell_id"):
                self.trader.check(ticker, "SELL", order["sell_id"])

    def order_checked(self, ticker, side, info, message):
        order = self.order(ticker)
        if info is None or not order:
            return                  # tried again at the next check
        if order.get("state") != ("buy_open" if side == "BUY" else "sell_open"):
            return                  # an older answer
        if side == "SELL" and str(info.get("orderId", order.get("sell_id"))) != str(order.get("sell_id")):
            return                  # about a sell order since replaced
        status = info.get("status", "")
        filled = int(info.get("filledQuantity") or 0)
        order["status"] = status
        if side == "BUY":
            order["filled"] = filled
            if status == "FILLED" or (status in ORDER_DONE and filled):
                order["state"] = "bought"
                order["bought"] = datetime.now().isoformat(timespec="seconds")
                order.pop("status", None)
                order["note"] = ("" if status == "FILLED" else
                                 f"The buy order was {status.lower()} after {filled:,} of {order['shares']:,}.")
            elif status in ORDER_DONE:
                self.close_order(ticker, f"buy {status.lower()}")
        else:
            order["sold"] = filled
            if status == "FILLED":
                self.close_order(ticker, "sold")
            elif status in ORDER_DONE and status != "REPLACED":
                order["state"] = "bought"       # SELL can send it again
                order.pop("status", None)
                order["filled"] = (order.get("filled") or order["shares"]) - filled    # what's left
                order["note"] = (f"The sell order was {status.lower()}"
                                 + (f" after {filled:,} were sold." if filled else "."))
        self.save_settings(self.settings)
        if ticker == self.ticker:
            self.refresh()

    def close_order(self, ticker, how):
        """The order is over (sold, or the buy didn't happen): kept in order_history, BUY works again."""
        order = self.settings.get("orders", {}).pop(ticker, None)
        if order:
            order.update(ticker=ticker, ended=datetime.now().isoformat(timespec="seconds"), how=how)
            self.settings.setdefault("order_history", []).append(order)

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
            self.bids, self.asks, self.l2_live = [], [], False
        self.rows, self.market_days, self.range, self.period = rows, market_days, rng, period
        self.read_fee()
        self.refresh()

    def set_quote(self, ticker, q):
        if ticker == self.ticker:
            self.bid = q.get("BID_PRICE") or self.bid
            self.ask = q.get("ASK_PRICE") or self.ask
            self.refresh()

    def set_book(self, ticker, bids, asks):
        if ticker == self.ticker:
            self.bids, self.asks, self.l2_live = bids, asks, True
            self.refresh()

    def clear_live(self):
        self.bid = self.ask = None
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
        shares = (order.get("filled") or order.get("shares")) if order else None
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
                       "From the order sent to Schwab" if order else f"round({usd:g} / entry)"),
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

    def refresh(self):
        if not self.widgets:
            return
        results = self.evaluate()
        order = self.order()
        self.update_trade_buttons()
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
                locked = order is not None and item.key == "entry"      # the price the buy was sent at
                fixed = order is not None and item.key in ("entry", "exit", "stop")
                if fixed:
                    tip = ("From the order sent to Schwab, not the live prices. "
                           + ("Fixed: the buy order's price." if locked else
                              "Change it to sell at another price." if item.key == "exit" else "You can change it."))
                value.setToolTip(tip)
                value.setReadOnly(locked)
                pal = value.palette()
                pal.setColor(QPalette.ColorRole.Text, QColor(ORDER_COLOR if fixed else "#f0a020"))
                value.setPalette(pal)
                tools = self.price_tools.get(item.key)
                if tools:
                    tools[0].setEnabled(not locked)
                    tools[1].setEnabled(not fixed)      # no going back to the live value
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
