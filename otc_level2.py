"""
otc_level2.py - Live Level II (market makers' bids and asks) from the Schwab API, for the Chart tab.

The Schwab stream itself is held by the server (otc_server.py, a background process the app starts):
ServerFeed tells it the ticker followed (the chart's) and the extra ones, and gets their Level II
(NASDAQ_BOOK and NYSE_BOOK; OTC stocks come on NASDAQ_BOOK) and Level 1 quotes for the header line.

Level2Panel shows it like thinkorswim's Level II: bids on the left, asks on the right, one row per
market maker, rows of the same price level sharing a color.

Reloaders: a yellow market maker (MM_YELLOW) that kept its offer on the lowest Ask while it was bought
from, in the last 5 market days' record (otc_recorder.find_reloads: trades at its price without its
amount getting smaller, or the amount dropping and quickly coming back), is a diluter: its sign turns
red with a yellow ring (level 3, a black "!"; level 4, a white "!", when at least half of its amount
was bought: a strong case) and the checklist counts it as one. On the lowest Ask price it is placed
first in the Ask table, then a yellow one not caught.

Hold at the close (on by default): outside the regular session (9:30-16:00 US Eastern, 16:30-23:00 in
Israel most of the year) the chart's ticker shows its Level 1 and Level II as they were at the last
close, from the record (the server's at_close), in gray: what streams after the close is ignored
(it often differs a lot). The chart and the checklist get those values too; the open ATM trades
keep the live ones. At the open the live stream is shown again.

The keys are in settings.json (schwab_app_key, schwab_app_secret, schwab_callback_url) and the
login token in schwab_token.json. The token lasts 7 days; then the panel asks to log in again.
schwab-py is imported only when needed, so the app runs without it.
"""

import re
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextDocument,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from otc_chart import GOLD, HELD, HELD_LEVELS, LEVEL_COLORS, fmt_price
from otc_recorder import RELOAD_DAYS, find_reloads, in_session, last_close

TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this

# LEVEL_COLORS and GOLD (otc_chart): thinkorswim's Level II colors, also the chart's Level II tags
GREEN = "#1db31d"       # a size that grew, a rise, the bid in the quote bar
RED = "#e0404f"         # a size that shrank, a fall
MINE = "#14692c"        # the row of an open order of mine, as thinkorswim marks it
LAST = "#f3c400"        # the last price
TEXT = "#d8d8d8"
SNAPSHOT_ROWS = 15      # the screenshot's Level II: at most this many rows a side
# Market makers to watch on the ask side, from the OTC course's market maker table. Green ones
# (retail: ETRF, CSTI, GTSM, NITE) get no sign.
MM_RED = {"MAXM", "GLED", "CFGN", "PAUL", "JANE", "BBAR", "BLAS", "ALPS", "STXG", "AEXG"}   # bad players: diluters
MM_YELLOW = {"OTCN", "OTCX", "CDEL", "INTL", "VIRT"}                                         # so-so
MM_TIPS = {4: "A dilutive market maker here: it refills its offer while it's bought from (at least half of its "
              "amount at a time)! Don't buy this stock!",
           3: "A dilutive market maker here: it refills its offer while it's bought from! Don't buy this stock!",
           2: "Dilutive Market Maker! Don't buy this stock!",
           1: "Might sometimes be a Dilutive Market Maker.\nIf it's first check if it auto refills the stocks.",
           5: "Probably Unsolicited Stock"}
BUYER = 5               # the sign (magenta) of CSTI / GTSM caught keeping / refilling its 10,000: "Probably Unsolicited Stock"
UNSOLICITED = {"CSTI", "GTSM"}      # the market makers watched for it (on both sides)...
UNSOLICITED_SIZE = 10_000           # ...showing this amount
SIGN_W = 16             # the Ask table's sign column
NET_COLORS = {"ok": "#1db31d", "bad": "#e0404f"}      # the pillow: the checklist's ratio ✔ / ✘ (else gray)
HOLD_CHECK_MS = 2000    # how often the market's hours are looked at (the hold starts / ends on time)
RELOAD_CHECK_MS = 60_000    # how often the shown ticker's record is looked at for reloaders, in the session
MARKET_LOG = Path(__file__).resolve().parent / "market_log"     # the server's record (otc_recorder)


def find_buyers(folder, ticker, days=RELOAD_DAYS):
    """{"BID" / "ASK": {market maker: its events}}: CSTI / GTSM caught keeping (or refilling) its 10,000 on the best
    price while traded with - the highest Bid while sold to, the lowest Ask while bought from (the diluter's check,
    otc_recorder.find_reloads): the Level II's magenta sign, "Probably Unsolicited Stock", the desktop's and the
    Mini App's."""
    out = {}
    for side in ("BID", "ASK"):
        found = out[side] = {}
        for ev in find_reloads(folder, ticker, UNSOLICITED, days, side=side, only_size=UNSOLICITED_SIZE):
            found.setdefault(ev["mm"], []).append(ev)
    return out


def unsolicited_tip(events, side):
    """The magenta sign's tooltip: what it means on that side, and when it happened."""
    how = ("keeps its bid (or refills it) while it's sold to, on the highest Bid" if side == "BID" else
           "keeps its offer (or refills it) while it's bought from, on the lowest Ask")
    return f"{MM_TIPS[BUYER]}: this market maker {how}.\n\n{reload_text(events, side)}"


def mm_risk(mm, reloaders):
    """A market maker's sign on the Ask (MM_TIPS' levels): 2 red (a diluter), 1 yellow (so-so), 3 / 4 a yellow one
    caught refilling (4: a strong case), 0 none. The Level II box's and the Mini App's."""
    caught = (reloaders or {}).get(mm)
    return 2 if mm in MM_RED else ((4 if any(e.get("strong") for e in caught) else 3) if caught else 1) \
        if mm in MM_YELLOW else 0


def reload_text(events, side="ASK"):
    """A reloader's events, for its tooltip: the last ones, newest first (side "BID": a buyer's, sold to it)."""
    lines = []
    for e in reversed(events[-4:]):
        when = datetime.fromtimestamp(e["t"] / 1000).strftime("%a %b %d, %H:%M:%S")
        how = ("its amount didn't get smaller" if e["kind"] == "kept" else
               f"its amount came back to {e['size']:,} within seconds")
        lines.append(f"{when}: {e['traded']:,} {'sold' if side == 'BID' else 'bought'} at {fmt_price(e['price'])} "
                     f"while it showed "
                     f"{e['size']:,}; {how}" + (" (strong)" if e.get("strong") else ""))
    more = f"\n(and {len(events) - 4} more)" if len(events) > 4 else ""
    return "\n".join(lines) + more

PANEL_STYLE = f"""
#level2 {{ background: #000000; }}
#l2bar {{ background: #333333; }}
#l2bar QLabel {{ color: {TEXT}; }}
#l2bar QLabel#l2cell {{ padding: 2px 9px; }}
#l2divider {{ background: #5c5c5c; margin: 3px 0; }}
#l2status {{ color: #9a9a9a; padding: 2px 9px; }}
QTableWidget {{ background: #000000; color: {GOLD}; border: none; gridline-color: #22303c; }}
QHeaderView::section {{ background: #151a1f; color: #c8c8c8; font-weight: normal;
    border: none; border-right: 1px solid #2a3540; border-bottom: 1px solid #2a3540; padding: 2px 4px; }}
QTableCornerButton::section {{ background: #151a1f; border: none; }}
"""


def tos_price(v, sign=False) -> str:
    """Prices as in the rest of the app: under $1 with 4 decimals (0.0415), from $1 with 2.
    With sign, a rise gets a + in front."""
    if v is None:
        return "-"
    return ("+" if sign and v > 0 else "") + fmt_price(v)


def token_created(token_path: Path):
    """When the browser login was made (time.time() seconds), or None without a usable token file."""
    try:
        return float(json.loads(token_path.read_text(encoding="utf-8"))["creation_timestamp"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def token_age_days(token_path: Path):
    """Days since the last browser login, or None without a usable token file."""
    try:
        created = json.loads(token_path.read_text(encoding="utf-8"))["creation_timestamp"]
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    return (time.time() - created) / 86400


def write_token(token_path: Path):
    """The token writer schwab-py expects, in the file format its token-file login reads. schwab-py
    hands it the token already wrapped as {"creation_timestamp", "token"}: written as it is (a bare
    token gets wrapped, for safety; wrapping a wrapped one made a file no login could read)."""
    def write(token, *args, **kwargs):
        if not (isinstance(token, dict) and "creation_timestamp" in token and "token" in token):
            token = {"creation_timestamp": int(time.time()), "token": token}
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token), encoding="utf-8")
        tmp.replace(token_path)
    return write


class ServerFeed(QObject):
    """Level II and Level 1 from the server (otc_server.py), which holds the Schwab stream: the ticker
    followed (the chart's) and any extra ones (the open ATM trades). The signals arrive in the GUI
    thread, for every ticker."""
    book = pyqtSignal(str, list, list)     # ticker, bids, asks: [(market maker, price, size, level)]
    quote = pyqtSignal(str, dict)          # ticker, Level 1 fields so far (stream names)
    status = pyqtSignal(str)               # what the connection is doing
    login_needed = pyqtSignal(str)         # why
    order_event = pyqtSignal(str, str)     # the account activity stream: an event type, its Schwab order id
    activity = pyqtSignal(bool)            # the account activity stream is on (True) or off
    streamed = pyqtSignal(int, int, int)   # tickers streamed, wanted, Schwab's most
    closed = pyqtSignal(dict)              # a ticker's market at the last close (the server's at_close)

    def __init__(self, link):
        super().__init__()
        self.link = link
        self._want = None                  # the ticker to follow, None for none
        self._extra = []                   # more tickers to stream (the open ATM trades)
        self._track = []                   # more to stream for the record only (favorites...)
        self._on = False                   # streaming wanted at all
        link.message.connect(self._on_message)
        link.connected.connect(self._on_connected)

    def set_symbol(self, ticker):
        self._want = ticker or None
        if ticker:
            self._on = True
        self._send()

    def set_extra(self, tickers):
        """More tickers to stream besides the one followed."""
        self._extra = [t for t in tickers if t]
        if self._extra:
            self._on = True
        self._send()

    def set_track(self, tickers):
        """More tickers to stream for the record only (in order of priority)."""
        self._track = [t for t in tickers if t]
        if self._track:
            self._on = True
        self._send()

    def start(self):
        self._on = True
        self._send()

    def stop(self):
        self._on = False
        self._send()

    def restart(self):
        """After a new login: the server starts its stream over by itself."""

    def close(self):
        """No stream wanted, and no more messages to this feed."""
        self.stop()
        for signal, slot in ((self.link.message, self._on_message), (self.link.connected, self._on_connected)):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    def _send(self):
        self.link.want(self._want, self._extra, self._track, self._on)

    def _on_connected(self, ok):
        if not ok:
            self.activity.emit(False)
            self.status.emit("The Schwab connection stopped: starting it again...")

    def _on_message(self, msg):
        kind = msg.get("type")
        if kind == "quote":
            self.quote.emit(msg["ticker"], msg["q"])
        elif kind == "book":
            self.book.emit(msg["ticker"], [tuple(r) for r in msg["bids"]], [tuple(r) for r in msg["asks"]])
        elif kind == "status":
            self.status.emit(msg["text"])
        elif kind == "login_needed":
            self.login_needed.emit(msg["why"])
        elif kind == "order_event":
            self.order_event.emit(msg["kind"], msg["order_id"])
        elif kind == "activity":
            self.activity.emit(bool(msg["on"]))
        elif kind == "streamed":
            self.streamed.emit(int(msg["count"]), int(msg["wanted"]), int(msg["max"]))
        elif kind == "at_close":
            self.closed.emit(msg)
        elif kind == "welcome":
            self.activity.emit(bool(msg.get("activity")))
            if msg.get("login_needed"):
                self.login_needed.emit(msg["login_needed"])
            elif msg.get("status"):
                self.status.emit(msg["status"])


class ElidedLabel(QLabel):
    """A label of plain text that ends in "..." when its room is short (the full text on hover): it never
    makes the window wider than the screen."""

    full = ""

    def setText(self, text):
        self.full = text or ""
        self.setToolTip(self.full)
        self.fit()
        self.updateGeometry()

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.full) + 24, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(40, super().minimumSizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()

    def fit(self):
        room = max(self.contentsRect().width() - 20, 0)        # (the style's padding)
        super().setText(self.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideRight, room)
                        if self.fontMetrics().horizontalAdvance(self.full) > room else self.full)


def pillow_icon(color: str) -> QIcon:
    """A pillow (the checklist's safety net), in this color."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()           # a cushion: its sides pinched in, its corners sticking out
    path.moveTo(1, 3)
    path.quadTo(16, 8, 31, 3)
    path.quadTo(27, 16, 31, 29)
    path.quadTo(16, 24, 1, 29)
    path.quadTo(5, 16, 1, 3)
    p.setPen(QPen(QColor(color).darker(150), 1.5))
    p.setBrush(QColor(color))
    p.drawPath(path)
    p.setPen(QPen(QColor(255, 255, 255, 110), 1.5))     # a seam
    seam = QPainterPath()
    seam.moveTo(7, 10)
    seam.quadTo(16, 13, 25, 10)
    p.drawPath(seam)
    p.end()
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Selected)
    return icon


def pair_icon(left: QIcon, right: QIcon) -> QIcon:
    """Two signs side by side (the wide sign column)."""
    pix = QPixmap(68, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.drawPixmap(0, 0, left.pixmap(32, 32))
    p.drawPixmap(36, 0, right.pixmap(32, 32))
    p.end()
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Selected)
    return icon


class Level2Panel(QWidget):
    live_quote = pyqtSignal(str, dict)            # ticker, Level 1 fields: for the chart's price and volume tags
    book_changed = pyqtSignal(str, list, list)    # ticker, bids, asks, as shown
    any_quote = pyqtSignal(str, dict)             # the same for every ticker streamed (the ATM trades' too)
    any_book = pyqtSignal(str, list, list)
    logged_in = pyqtSignal()                      # a new browser login was saved to the token file
    order_event = pyqtSignal(str, str)            # the account activity stream's event type, Schwab order id
    activity_live = pyqtSignal(bool)              # the account activity stream is on / off
    streamed = pyqtSignal(int, int, int)          # tickers streamed, wanted, Schwab's most
    held_changed = pyqtSignal(bool)               # showing the last close (True), or the live stream again
    reloaders_changed = pyqtSignal(str, dict)     # ticker, {yellow market maker: its reload events}
    _reloads_found = pyqtSignal(str, list, dict)  # (from the reading thread: the reloads, the buyers)
    market_reset = pyqtSignal()                   # the shown ticker's quote and book start over (held / live)

    """The Level II box under the chart, styled after thinkorswim: a quote bar, then bids and asks
    side by side on black, one row per market maker, darker blue for each price level further out."""

    def __init__(self, settings: dict, token_path: Path, link, mm_icons=None, parent=None):
        """link: the app's ServerLink (otc_server_link). mm_icons: {1: yellow sign, 2: red sign} for
        the market makers in MM_YELLOW / MM_RED."""
        super().__init__(parent)
        self.settings, self.token_path, self.link = settings, token_path, link
        self.mm_icons = mm_icons or {}
        self.ticker = ""
        self.name = ""
        self.feed = None
        # my open orders on a ticker, for their own green rows in the book, as thinkorswim shows them:
        # ticker -> [{"side", "price", "shares", "account", ...}] (set by the app)
        self.my_orders = lambda ticker: []
        self.shown_book = ([], [])             # the shown ticker's bids and asks, as streamed
        self.extra = []                        # the open ATM trades' tickers, streamed too
        self.tracked = []                      # more tickers streamed for the record (favorites...)
        self.off = False                       # Level II turned off: no stream at all
        self.prev = {"BID": {}, "ASK": {}}     # market maker -> last size, per side
        self.shown_rows = {"BID": [], "ASK": []}   # the rows as the tables show them now (the screenshot's)
        self.trend = {"BID": {}, "ASK": {}}    # market maker -> +1/-1, the last size change
        self.prev_quote = {}                   # bid/ask price and size at the last quote update
        self.quote_trend = {}                  # field -> +1/-1: its last change (kept until it changes again)
        # Hold at the close: outside the market's hours, the last close instead of the stream
        self.hold = bool(settings.get("hold_close", True))
        self.held = False                      # showing the close now
        self.close_info = None                 # the shown ticker's close (the server's answer), when held
        self.reloaders = {}                    # yellow market maker -> its reload events (the shown ticker)
        self.buyers = {}                       # "BID" / "ASK" -> {CSTI / GTSM: its events} (the shown ticker)
        self.net = ("", None, "")             # the checklist's (ticker, safety net, ratio state): the Bid's pillow
        self.market_log = MARKET_LOG
        self.live_q = {}                       # the shown ticker's latest streamed quote and book
        self.live_book = ([], [])
        self.live_status = ""
        self.setObjectName("level2")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("l2bar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        head = QHBoxLayout(bar)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(0)
        self.cells = {}
        tips = {
            "last": "Last: the price of the last trade.",
            "last_change": "Last price's change from yesterday's close.",
            "mark": "Mark: the price the position is valued at. During regular hours it's the last price; "
                    "outside them it can differ.",
            "mark_change": "Mark's change from yesterday's close.",
        }
        self.dividers = {}
        for key in ("ticker", "name", "last", "last_change", "mark", "mark_change", "bid", "ask"):
            lb = QLabel()
            lb.setObjectName("l2cell")
            lb.setTextFormat(Qt.TextFormat.RichText)
            lb.setToolTip(tips.get(key, ""))
            self.cells[key] = lb
            if key == "name":           # a long name: cut when the room is short (not the window wider)
                lb.setMinimumWidth(30)
            head.addWidget(lb)
            # A thin light gray line after each group, as in thinkorswim. A widget of its own: the
            # Windows 11 style doesn't draw borders on labels.
            line = QWidget()
            line.setObjectName("l2divider")
            line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            line.setFixedWidth(1)
            self.dividers[key] = line
            head.addWidget(line)
        head.addStretch()
        self.login_btn = QPushButton("Log in to Schwab...")
        self.login_btn.clicked.connect(self.login)
        self.login_btn.hide()
        head.addWidget(self.login_btn)
        self.status_label = ElidedLabel()      # (shortened when the room is short)
        self.status_label.setObjectName("l2status")
        head.addWidget(self.status_label)
        root.addWidget(bar)

        sides = QHBoxLayout()
        sides.setContentsMargins(0, 0, 0, 0)
        sides.setSpacing(6)
        self.bid_table = self._table(["", "Ex", "Bid", "BS"], sign_col=True, bid=True)
        self.ask_table = self._table(["", "Ex", "Ask", "AS"], sign_col=True)
        sides.addWidget(self.bid_table)
        sides.addWidget(self.ask_table)
        root.addLayout(sides, 1)
        self.show_quote({})
        self.hold_timer = QTimer(self)
        self.hold_timer.timeout.connect(self.check_hold)
        self.hold_timer.start(HOLD_CHECK_MS)
        self._reloads_found.connect(self._on_reloads)
        self.reload_timer = QTimer(self)
        self.reload_timer.timeout.connect(lambda: in_session() and self.check_reloads())
        self.reload_timer.start(RELOAD_CHECK_MS)

    @staticmethod
    def _table(titles, sign_col=False, bid=False):
        """sign_col: a narrow first column for the market maker signs."""
        t = QTableWidget(0, len(titles))
        t.setHorizontalHeaderLabels(titles)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Compact rows, as in thinkorswim; the style's minimum row height would make them half again as tall
        rows = t.verticalHeader()
        rows.setVisible(False)
        rows.setMinimumSectionSize(1)
        rows.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        rows.setDefaultSectionSize(t.fontMetrics().height() + 4)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hdr.setHighlightSections(False)
        first = 1 if sign_col else 0
        if sign_col:
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            hdr.setMinimumSectionSize(SIGN_W)     # the style's minimum would keep it twice as wide
            hdr.resizeSection(0, SIGN_W)
            t.setIconSize(QSize(SIGN_W - 4, SIGN_W - 4))
            t.horizontalHeaderItem(0).setToolTip(
                "Pillow: the checklist's safety net (green: the profit-to-loss ratio reaches its target, red: it "
                "doesn't). Magenta: Probably Unsolicited Stock (CSTI / GTSM keeps or refills its 10,000 while it's "
                "sold to)."
                if bid else "Red: a Dilutive Market Maker. Yellow: might sometimes be a Dilutive Market Maker. "
                            "Magenta: Probably Unsolicited Stock (CSTI / GTSM keeps or refills its 10,000 while it's "
                            "bought from).")
        t.horizontalHeaderItem(first).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col in (first + 1, first + 2):
            t.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return t

    # ------------------------------------------------------------ control ---
    def keys(self):
        return (self.settings.get("schwab_app_key", "").strip(), self.settings.get("schwab_app_secret", "").strip(),
                self.settings.get("schwab_callback_url", "https://127.0.0.1").strip())

    def follow(self, ticker: str, name: str = ""):
        """Show this ticker's Level II; an empty ticker pauses the stream."""
        if ticker != self.ticker:
            self.prev = {"BID": {}, "ASK": {}}
            self.trend = {"BID": {}, "ASK": {}}
            self.prev_quote = {}
            self.quote_trend = {}
            self.ticker, self.name = ticker, name
            self.shown_book = ([], [])
            self.live_q, self.live_book, self.close_info = {}, ([], []), None
            self.reloaders, self.buyers = {}, {}
            self.reloaders_changed.emit(ticker, {})
            self.check_reloads()
            self.refresh_mine()                # my orders show until the book comes
            self.show_quote({})
            if self.held:
                self.ask_close()
        if not ticker:
            if self.feed:
                self.feed.set_symbol(None)
            return
        self.off = False
        self._make_feed()
        if self.feed:
            self.feed.set_symbol(ticker)

    def watch(self, tickers):
        """Streams these tickers too (the open ATM trades), while Level II is on."""
        self.extra = list(tickers)
        if self.extra and not self.off:
            self._make_feed()
        if self.feed:
            self.feed.set_extra(self.extra)

    def track(self, tickers):
        """Streams these tickers too, for the record only (recorded by the server, not sent here),
        in order of priority (Schwab streams up to 500 in all)."""
        self.tracked = list(tickers)
        if self.tracked and not self.off:
            self._make_feed()
        if self.feed:
            self.feed.set_track(self.tracked)

    def _make_feed(self):
        key, secret, _ = self.keys()
        if not key or not secret:
            self.set_status("Add schwab_app_key and schwab_app_secret to settings.json to see Level II.")
            return
        if self.feed is None:
            self.feed = ServerFeed(self.link)
            self.feed.book.connect(self.show_book)
            self.feed.quote.connect(self.on_quote)
            self.feed.status.connect(self.set_status)
            self.feed.login_needed.connect(self.on_login_needed)
            self.feed.order_event.connect(self.order_event)
            self.feed.activity.connect(self.activity_live)
            self.feed.streamed.connect(self.streamed)
            self.feed.closed.connect(self.on_close)
            self.feed.set_extra(self.extra)
            self.feed.set_track(self.tracked)

    # ------------------------------------------------------------ reloaders ---
    def check_reloads(self):
        """Reads the shown ticker's last market days' record for reloaders, in a thread."""
        ticker, folder = self.ticker, self.market_log
        if not ticker:
            return

        def run():
            try:
                events = find_reloads(folder, ticker, MM_YELLOW, RELOAD_DAYS)
            except Exception:
                events = []
            try:
                buyers = find_buyers(folder, ticker, RELOAD_DAYS)
            except Exception:
                buyers = {}
            try:
                self._reloads_found.emit(ticker, events, buyers)
            except RuntimeError:        # (the panel is gone: the app closed)
                pass
        threading.Thread(target=run, daemon=True).start()

    def _on_reloads(self, ticker, events, buyers):
        if ticker != self.ticker:
            return
        found = {}
        for e in events:
            found.setdefault(e["mm"], []).append(e)
        if buyers != self.buyers:
            self.buyers = buyers
            if found == self.reloaders:
                self.refresh_mine()
        if found != self.reloaders:
            self.reloaders = found
            self.refresh_mine()
            self.reloaders_changed.emit(ticker, found)

    def on_quote(self, ticker, q):
        self.any_quote.emit(ticker, q)      # (the ATM trades: always the live market)
        if ticker == self.ticker:
            self.live_q = q
            if not self.held:
                self.show_quote(q)
                self.live_quote.emit(ticker, q)

    # ------------------------------------------------------------ hold at the close ---
    def set_hold(self, on):
        self.hold = bool(on)
        self.check_hold()

    def check_hold(self):
        """Holds the close outside the market's hours (when on), and goes back to the stream in them."""
        held = self.hold and not in_session()
        if held == self.held:
            if held and self.close_info is not None and self.close_info.get("day") != self._close_day():
                self.ask_close()        # (the next close passed while held: a new day's)
            return
        self.held = held
        self.held_changed.emit(held)
        self.prev = {"BID": {}, "ASK": {}}
        self.trend = {"BID": {}, "ASK": {}}
        self.prev_quote = {}
        self.quote_trend = {}
        if held:
            self.close_info = None
            self.market_reset.emit()
            self.shown_book = ([], [])
            self.show_quote({})
            self.refresh_mine()
            self.ask_close()
        else:       # the stream's latest again
            self.close_info = None
            self.shown_book = self.live_book
            self.show_quote(self.live_q)
            self.refresh_mine()
            self.status_label.setText(self.live_status)
            self.market_reset.emit()
            if self.ticker:
                self.live_quote.emit(self.ticker, self.live_q)
                self.book_changed.emit(self.ticker, *self.live_book)

    @staticmethod
    def _close_day():
        day, _ = last_close()
        return day.isoformat() if day else None

    def ask_close(self):
        """The shown ticker's last close, from this computer's server (on_close answers)."""
        if not self.ticker:
            return
        self._show_status(f"Held at the close · reading {self.ticker}'s close...")
        if not self.link.send({"type": "at_close", "ticker": self.ticker}):
            # the server isn't there yet (the app starting): again in a moment
            QTimer.singleShot(1000, lambda t=self.ticker: self.held and self.close_info is None
                              and t == self.ticker and self.ask_close())

    def on_close(self, msg):
        if not self.held or msg.get("ticker") != self.ticker:
            return
        self.close_info = msg
        self.market_reset.emit()
        q = msg.get("q") or {}
        bids = [tuple(r) for r in msg.get("bids") or []]
        asks = [tuple(r) for r in msg.get("asks") or []]
        self.shown_book = (bids, asks)
        self.show_quote(q)
        self.refresh_mine()
        close = msg.get("close")
        when = datetime.fromtimestamp(close / 1000).strftime("%a %b %d, %H:%M") if close else "?"
        if msg.get("q") is None:
            self._show_status(f"Held at the close ({when}) · {self.ticker} wasn't recorded before it")
        else:
            at = msg.get("at")
            self._show_status(f"Held at the close ({when})" +
                              (f" · last update {datetime.fromtimestamp(at / 1000):%H:%M:%S}" if at else ""))
        self.live_quote.emit(self.ticker, q)
        self.book_changed.emit(self.ticker, bids, asks)

    def _show_status(self, text):
        self.status_label.setText(f"Level II · {text}")

    def stop(self):
        if self.feed:
            self.feed.stop()

    def disconnect_feed(self):
        """Logs out of Schwab's stream and drops the connection, so another PC with the same
        login can stream (Schwab allows one stream per login). follow() connects again."""
        self.follow("")
        self.off = True
        if self.feed:
            feed, self.feed = self.feed, None
            for signal in (feed.book, feed.quote, feed.status, feed.login_needed, feed.order_event, feed.activity,
                           feed.streamed):
                try:
                    signal.disconnect()
                except TypeError:
                    pass
            feed.close()        # the server logs out of the stream
            self.activity_live.emit(False)
        self.login_btn.hide()
        self.set_status("Off · not connected to Schwab")

    def set_status(self, text):
        self.live_status = f"Level II · {text}"
        if not self.held:
            self.status_label.setText(self.live_status)

    def on_login_needed(self, why):
        self.set_status(why)
        self.login_btn.show()

    def login(self):
        key, secret, callback = self.keys()
        try:
            from schwab.auth import client_from_received_url, get_auth_context
        except ImportError:
            QMessageBox.warning(self, "schwab-py missing", "Run  pip install schwab-py  and restart the app.")
            return
        ctx = get_auth_context(key, callback)
        QDesktopServices.openUrl(QUrl(ctx.authorization_url))
        url, ok = QInputDialog.getText(
            self, "Log in to Schwab",
            "A Schwab login page opened in your browser.\n\n"
            "1. Log in with your regular Schwab brokerage login.\n"
            "2. Select your accounts and click Allow.\n"
            "3. The browser ends on a page that can't be reached (127.0.0.1). That's expected.\n"
            "4. Copy the FULL address from the address bar, paste it here and click OK.\n\n"
            "Do it quickly: the code in the address runs out after about 30 seconds.")
        url = url.strip()
        if not ok or not url:
            return
        def to_server(wrapped, *args, **kwargs):
            # the server keeps the token (and renews it): sent to it; saved here only without a server
            if not self.link.login(wrapped):
                write_token(self.token_path)(wrapped)
        try:
            client_from_received_url(key, secret, ctx, url, to_server)
        except Exception as e:
            QMessageBox.warning(self, "Schwab login failed",
                                f"{e}\n\n\"invalid_grant\" usually means the address was pasted too late. Try again.")
            return
        self.login_btn.hide()
        self.logged_in.emit()
        self.set_status("Logged in. Connecting...")
        if self.feed:
            self.feed.restart()
        else:
            self.follow(self.ticker, self.name)

    # ------------------------------------------------------------ display ---
    def show_quote(self, q: dict):
        c = self.cells
        c["ticker"].setText(self.ticker or "Level II")
        c["name"].setText(self.name[:24])
        # Last and Mark, each with its change from yesterday's close, as in thinkorswim
        close = q.get("CLOSE_PRICE")
        for key, price_key, change_key in (("last", "LAST_PRICE", "NET_CHANGE"), ("mark", "MARK", "MARK_CHANGE")):
            price = q.get(price_key)
            change = q.get(change_key)
            if change is None and price is not None and close:
                change = price - close
            c[key].setText(f"<span style='color:{LAST}; font-size:13pt; font-weight:bold'>{tos_price(price)}</span>"
                           if price is not None else "")
            c[f"{key}_change"].setText(self._change_text(change, close))
        # Bid and ask price and size, each on its own: green after it went up, red after it went down
        # (kept until that value changes again, as the book's sizes), yellow before any change
        def tone(field):
            if self.held:
                return HELD
            now, before = q.get(field), self.prev_quote.get(field)
            if now is not None and before is not None and now != before:
                self.quote_trend[field] = 1 if now > before else -1
            return {1: GREEN, -1: RED}.get(self.quote_trend.get(field), LAST)
        for key, side in (("bid", "BID"), ("ask", "ASK")):
            price = q.get(f"{side}_PRICE")
            size = q.get(f"{side}_SIZE")    # not known yet (or 0) while waiting for the book: N/A
            size = f"{size:,}" if size else "N/A"
            c[key].setText(f"{side.title()}: <span style='color:{tone(side + '_PRICE')}'>{tos_price(price)}</span> x "
                           f"<span style='color:{tone(side + '_SIZE')}'>{size}</span>" if price is not None else "")
        self.prev_quote = {f: q[f] for f in ("BID_PRICE", "BID_SIZE", "ASK_PRICE", "ASK_SIZE") if f in q}
        for key, lb in c.items():
            lb.setVisible(bool(lb.text()))
            self.dividers[key].setVisible(bool(lb.text()))

    def still(self, ticker, name, q, bids, asks, held=False, reloaders=None):
        """A hidden panel with no stream (the Mini App's screenshot): the ticker's quote and book as given (what the
        Level II shows for it now), its rows as the tables would show them (my orders' too), for snapshot()."""
        self.prev = {"BID": {}, "ASK": {}}
        self.trend = {"BID": {}, "ASK": {}}
        self.prev_quote, self.quote_trend = {}, {}
        self.ticker, self.name = ticker, name or ""
        self.reloaders = reloaders or {}
        self.held = False
        self.show_quote(q or {})
        self.shown_book = (list(bids), list(asks))
        self.refresh_mine()
        self.held = held

    def snapshot(self, width, max_rows=SNAPSHOT_ROWS):
        """The Level II as a picture (the chart's screenshot, under it): the quote bar's ticker, Bid and Ask (their
        colors as now), then the bids and asks as the tables show them, in the price levels' blue shades (also
        while it holds the last close) - no signs, no status line."""
        font = QFont(self.bid_table.font())
        fm = QFontMetrics(font)
        row_h = fm.height() + 4
        # the quote bar's cells as they show (rich text: their colors), each its own document
        docs = []
        for key in ("ticker", "name", "last", "last_change", "mark", "mark_change", "bid", "ask"):
            html = self.cells[key].text() if key in self.cells else ""
            if key == "ticker":
                html = f"<b>{self.ticker}</b>"
            elif key in ("bid", "ask"):     # (the screenshot's Bid and Ask: their price and size yellow)
                html = re.sub(r"color:[^'\";]+", f"color:{GOLD}", html)
            if not html:
                continue
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(font)
            doc.setDefaultStyleSheet(f"body {{ color: {TEXT}; }}")
            doc.setHtml(f"<body>{html}</body>")
            docs.append(doc)
        bar_h = max([fm.height() + 12] + [round(d.size().height()) + 8 for d in docs])
        n = min(max(len(self.shown_rows["BID"]), len(self.shown_rows["ASK"]), 1), max_rows)
        img = QImage(width, bar_h + row_h * (n + 1) + 4, QImage.Format.Format_RGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setFont(font)
        p.fillRect(0, 0, width, bar_h, QColor("#333333"))
        x = 0
        for doc in docs:
            x += 9
            p.save()
            p.translate(x, (bar_h - doc.size().height()) / 2)
            doc.drawContents(p)
            p.restore()
            x += doc.idealWidth() + 9
            p.fillRect(int(x), 3, 1, bar_h - 6, QColor("#5c5c5c"))
        p.setPen(QColor("#9a9a9a"))         # (thinkorswim's: the panel's name at the right)
        p.drawText(QRectF(x, 0, width - x - 12, bar_h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "Level II")
        # the two tables, side by side (6 px apart, as the panel)
        half = (width - 6) // 2
        for side, left, titles in (("BID", 0, ("Ex", "▾  Bid", "BS")), ("ASK", half + 6, ("Ex", "▴  Ask", "AS"))):
            cols = (left, left + half * 0.36, left + half * 0.68, left + half)
            y = bar_h
            p.fillRect(left, y, half, row_h, QColor("#151a1f"))
            p.setPen(QColor("#c8c8c8"))
            for c, title in enumerate(titles):
                p.drawText(QRectF(cols[c] + 4, y, cols[c + 1] - cols[c] - 8, row_h),
                           (Qt.AlignmentFlag.AlignLeft if c == 0 else Qt.AlignmentFlag.AlignRight)
                           | Qt.AlignmentFlag.AlignVCenter, title)
            for mm, price, size, level, mine in self.shown_rows[side][:n]:
                y += row_h
                p.fillRect(left, y, half, row_h - 1, QColor(MINE if mine else LEVEL_COLORS[min(level, len(LEVEL_COLORS) - 1)]))
                trend = 0 if mine or self.held else self.trend[side].get(mm, 0)
                texts = ((mm, GOLD), (tos_price(price), GOLD), (f"{size:,}", GREEN if trend > 0 else RED if trend < 0 else GOLD))
                for c, (text, color) in enumerate(texts):
                    p.setPen(QColor(color))
                    p.drawText(QRectF(cols[c] + 4, y, cols[c + 1] - cols[c] - 8, row_h),
                               (Qt.AlignmentFlag.AlignLeft if c == 0 else Qt.AlignmentFlag.AlignRight)
                               | Qt.AlignmentFlag.AlignVCenter, text)
        p.end()
        return img

    @staticmethod
    def _change_text(change, close):
        """The change and its %, on two small lines as in thinkorswim."""
        if change is None:
            return ""
        color = GREEN if change > 0 else RED if change < 0 else TEXT
        if not close:
            return f"<span style='color:{color}'>{tos_price(change, sign=True)}</span>"
        return (f"<div style='color:{color}; font-size:7pt'>{tos_price(change, sign=True)}</div>"
                f"<div style='color:{color}; font-size:7pt'>{change / close * 100:+.2f}%</div>")

    def show_book(self, ticker, bids, asks):
        self.any_book.emit(ticker, bids, asks)
        if ticker != self.ticker:
            return
        self.live_book = (bids, asks)
        if self.held:
            return
        self.shown_book = (bids, asks)
        self.refresh_mine()
        # without my rows: the chart and the checklist count only the market's
        self.book_changed.emit(ticker, bids, asks)

    def set_net(self, ticker, price, state):
        """The checklist's safety net and its profit-to-loss ratio's state (ok / bad / else): the Bid's pillow."""
        net = (ticker, price, state)
        if net != self.net:
            self.net = net
            if ticker == self.ticker:
                self.refresh_mine()

    def net_row(self, rows):
        """The Bid row for the pillow: the first of the market's at or below the checklist's safety net."""
        ticker, price, _state = self.net
        if ticker != self.ticker or price is None:
            return None
        return next((r for r, row in enumerate(rows) if not row[4] and row[1] <= price + 1e-12), None)

    def refresh_mine(self):
        """Fills the tables again: the book with my open orders' rows (after an order changed)."""
        bids, asks = self.shown_book
        orders = self.my_orders(self.ticker) if self.ticker else []
        self._fill(self.bid_table, self._with_mine(bids, orders, "BID"), "BID")
        self._fill(self.ask_table, self._with_mine(self.diluters_first(asks, self.reloaders), orders, "ASK"), "ASK")

    @staticmethod
    def diluters_first(asks, reloaders):
        """The Ask rows with the lowest ask price's suspects placed first, so they're seen at the top: a
        market maker caught refilling its offer (a reloader), then a yellow one not caught (so-so)."""
        best = min((row[1] for row in asks), default=None)
        caught = [row for row in asks if row[1] == best and row[0] in reloaders]
        yellow = [row for row in asks if row[1] == best and row[0] in MM_YELLOW and row[0] not in reloaders]
        top = caught + yellow
        return top + [row for row in asks if row not in top] if top else list(asks)

    @staticmethod
    def _with_mine(rows, orders, side):
        """The book's rows, (mm, price, size, level, mine), with a row for each of my open orders on
        this side, after the market makers at its price, as thinkorswim places them."""
        out = [(*row, False) for row in rows]
        for o in orders:
            if (o["side"] == "BUY") != (side == "BID") or not o.get("price") or o.get("shares", 0) <= 0:
                continue
            price = o["price"]
            at = 0
            while at < len(out) and (out[at][1] >= price if side == "BID" else out[at][1] <= price):
                at += 1
            level = out[at - 1][3] if at else 0
            out.insert(at, ("Me...", price, int(o["shares"]), level, True))     # (short: the chosen account's)
        return out

    def _fill(self, table, rows, side):
        self.shown_rows[side] = list(rows)
        prev, trend = self.prev[side], self.trend[side]
        table.setRowCount(len(rows))
        net_at = self.net_row(rows) if side == "BID" else None
        seen = self.buyers.get(side, {})
        both = net_at is not None and rows[net_at][0] in seen and BUYER in self.mm_icons
        # the sign column: twice as wide only while a row has both signs (the pillow and the magenta)
        width = 2 * SIGN_W - 2 if both else SIGN_W
        if table.horizontalHeader().sectionSize(0) != width:
            table.horizontalHeader().resizeSection(0, width)
            table.setIconSize(QSize(width - 4, SIGN_W - 4))
        for r, (mm, price, size, level, mine) in enumerate(rows):
            # Size colored by its last change, as in thinkorswim: green up, red down
            if not mine and not self.held:
                if mm in prev and size != prev[mm]:
                    trend[mm] = 1 if size > prev[mm] else -1
                prev[mm] = size
            shades = HELD_LEVELS if self.held else LEVEL_COLORS     # gray: the last close, not live
            bg = QColor(MINE if mine else shades[min(level, len(shades) - 1)])
            cells = [QTableWidgetItem(mm), QTableWidgetItem(tos_price(price)), QTableWidgetItem(f"{size:,}")]
            if mine:
                cells[0].setToolTip("My open order (from Schwab; not part of the market's book)")
            # The sign column. The Ask: red for a diluter, yellow for a so-so market maker, red with a
            # yellow ring for a yellow one caught refilling its offer (a diluter here). The Bid: magenta for a
            # CSTI / GTSM caught keeping / refilling its 10,000 while traded with, on either side
            # ("Probably Unsolicited Stock")
            sign = QTableWidgetItem()
            if side == "ASK":
                risk = mm_risk(mm, self.reloaders)
                if risk in self.mm_icons:
                    sign.setIcon(self.mm_icons[risk])
                    sign.setToolTip(MM_TIPS[risk] if risk < 3 else
                                    f"{MM_TIPS[risk]}\n\n{reload_text(self.reloaders[mm])}")
            if not mine and not sign.toolTip() and mm in seen and BUYER in self.mm_icons:
                sign.setIcon(self.mm_icons[BUYER])          # (magenta: CSTI / GTSM keeping its 10,000)
                sign.setToolTip(unsolicited_tip(seen[mm], side))
            if r == net_at:         # the checklist's safety net: a pillow, beside the magenta sign when both
                _t, net, state = self.net
                pillow = pillow_icon(NET_COLORS.get(state, "#8a8a8a"))
                tip = (f"The checklist's safety net: {fmt_price(net)}\n"
                       + {"ok": "Green: the profit-to-loss ratio reaches its target",
                          "bad": "Red: the profit-to-loss ratio doesn't reach its target"}.get(state, "The ratio isn't known"))
                sign.setIcon(pair_icon(pillow, sign.icon()) if sign.toolTip() else pillow)
                sign.setToolTip(f"{tip}\n\n{sign.toolTip()}" if sign.toolTip() else tip)
            cells.insert(0, sign)
            first = 1
            for col, item in enumerate(cells):
                item.setBackground(bg)
                item.setForeground(QColor(HELD if self.held and not mine else GOLD))
                if col > first:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, col, item)
            if not mine and not self.held and trend.get(mm):
                cells[-1].setForeground(QColor(GREEN if trend[mm] > 0 else RED))
