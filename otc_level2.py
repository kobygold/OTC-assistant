"""
otc_level2.py - Live Level II (market makers' bids and asks) from the Schwab API, for the Chart tab.

Level2Feed keeps one Schwab streaming connection in a background thread and follows one ticker
at a time: when the chart opens another ticker it unsubscribes the old one and subscribes the new
one. It streams NASDAQ_BOOK and NYSE_BOOK (OTC stocks come on NASDAQ_BOOK), and Level 1 quotes
for the header line. Every book message is a full snapshot of that side's book, not a change.

Level2Panel shows it like thinkorswim's Level II: bids on the left, asks on the right, one row per
market maker, rows of the same price level sharing a color.

The keys are in settings.json (schwab_app_key, schwab_app_secret, schwab_callback_url) and the
login token in schwab_token.json. The token lasts 7 days; then the panel asks to log in again.
schwab-py is imported only when needed, so the app runs without it.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from otc_chart import fmt_price

TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this
BOOK_SERVICES = ("NASDAQ_BOOK", "NYSE_BOOK")
RECONNECT_SECONDS = 15
POLL_SECONDS = 0.3              # how often the stream loop checks for a new ticker

# thinkorswim's Level II colors: one blue per price level, best first and darker further out,
# the same on both sides
LEVEL_COLORS = ["#265883", "#1f4669", "#193854", "#132d43", "#102436", "#0d1c2b"]
GOLD = "#e3b23c"        # market makers, prices and sizes
GREEN = "#1db31d"       # a size that grew, a rise, the bid in the quote bar
RED = "#e0404f"         # a size that shrank, a fall
LAST = "#f3c400"        # the last price
TEXT = "#d8d8d8"
# Market makers to watch on the ask side, from the OTC course's market maker table. Green ones
# (retail: ETRF, CSTI, GTSM, NITE) get no sign.
MM_RED = {"MAXM", "GLED", "CFGN", "PAUL", "JANE", "BBAR", "BLAS", "ALPS", "STXG", "AEXG"}   # bad players: diluters
MM_YELLOW = {"OTCN", "OTCX", "CDEL", "INTL", "VIRT"}                                         # so-so
MM_TIPS = {2: "Dilutive Market Maker! Don't buy this stock!",
           1: "Might sometimes be a Dilutive Market Maker.\nIf it's first check if it auto refills the stocks."}
SIGN_W = 16             # the Ask table's sign column

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


def token_age_days(token_path: Path):
    """Days since the last browser login, or None without a usable token file."""
    try:
        created = json.loads(token_path.read_text(encoding="utf-8"))["creation_timestamp"]
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    return (time.time() - created) / 86400


def write_token(token_path: Path):
    """The token writer schwab-py expects, in the file format its token-file login reads."""
    def write(token, *args, **kwargs):
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"creation_timestamp": int(time.time()), "token": token}), encoding="utf-8")
        tmp.replace(token_path)
    return write


def _login_expired(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(s in text for s in ("invalid_grant", "refresh_token", "unauthorized", "invalid_client", "401"))


class Level2Feed(QObject):
    """Streams Level II and Level 1 for one ticker in a background thread. The signals arrive in
    the GUI thread."""
    book = pyqtSignal(str, list, list)     # ticker, bids, asks: [(market maker, price, size, level)]
    quote = pyqtSignal(str, dict)          # ticker, Level 1 fields so far (stream names)
    status = pyqtSignal(str)               # what the connection is doing
    login_needed = pyqtSignal(str)         # why

    def __init__(self, app_key: str, app_secret: str, token_path: Path):
        super().__init__()
        self.app_key, self.app_secret, self.token_path = app_key, app_secret, token_path
        self._want = None                  # the ticker to follow, None for none
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._current = None
        self._books = {}                   # service -> (bids, asks) of the current ticker
        self._l1 = {}

    def set_symbol(self, ticker):
        with self._lock:
            self._want = ticker or None
        if ticker and not (self._thread and self._thread.is_alive()):
            self.start()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="level2", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def restart(self):
        """After a new login: drop the old connection, if any, and connect again."""
        self.stop()
        if self._thread:
            self._thread.join(timeout=3)
        self.start()

    # ------------------------------------------------------------ thread ---
    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:      # never let the thread die silently
            self.status.emit(f"Level II stopped: {e}")

    async def _main(self):
        try:
            from schwab.auth import client_from_token_file
        except ImportError:
            self.status.emit("Level II needs schwab-py: run  pip install schwab-py  and restart the app.")
            return
        while not self._stop.is_set():
            age = token_age_days(self.token_path)
            if age is None:
                self.login_needed.emit("Log in to Schwab to see Level II.")
                return
            if age >= TOKEN_DAYS:
                self.login_needed.emit("The Schwab login has run out (it lasts 7 days). Log in again.")
                return
            try:
                self.status.emit("Connecting to Schwab...")
                client = client_from_token_file(str(self.token_path), self.app_key, self.app_secret)
                await self._stream(client)
            except Exception as e:
                if _login_expired(e):
                    self.login_needed.emit(f"Schwab didn't accept the login ({e}). Log in again.")
                    return
                self.status.emit(f"Level II disconnected ({type(e).__name__}: {e}). "
                                 f"Reconnecting in {RECONNECT_SECONDS} s...")
                for _ in range(RECONNECT_SECONDS * 10):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(0.1)

    async def _stream(self, client):
        from schwab.streaming import StreamClient
        stream = StreamClient(client)
        await stream.login()
        # Handlers before subscribing: messages without one are dropped
        stream.add_nasdaq_book_handler(lambda m: self._on_book("NASDAQ_BOOK", m))
        stream.add_nyse_book_handler(lambda m: self._on_book("NYSE_BOOK", m))
        stream.add_level_one_equity_handler(self._on_level1)
        self._current = None
        try:
            while not self._stop.is_set():
                with self._lock:
                    want = self._want
                if want != self._current:
                    await self._switch(client, stream, self._current, want)
                # Subscribing and reading share the socket, so the ticker check waits for a pause
                try:
                    await asyncio.wait_for(stream.handle_message(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            try:
                await asyncio.wait_for(stream.logout(), timeout=3)
            except Exception:
                pass

    async def _switch(self, client, stream, old, new):
        if old:
            await stream.nasdaq_book_unsubs([old])
            await stream.nyse_book_unsubs([old])
            await stream.level_one_equity_unsubs([old])
        self._current, self._books, self._l1 = new, {}, {}
        if not new:
            self.status.emit("Level II paused.")
            return
        # A quote right away, so the header has prices even when nothing is streaming
        try:
            r = await asyncio.to_thread(client.get_quote, new)
            q = r.json().get(new, {}).get("quote", {}) if r.status_code == 200 else {}
        except Exception:
            q = {}
        for rest, name in (("lastPrice", "LAST_PRICE"), ("bidPrice", "BID_PRICE"), ("askPrice", "ASK_PRICE"),
                           ("bidSize", "BID_SIZE"), ("askSize", "ASK_SIZE"), ("closePrice", "CLOSE_PRICE"),
                           ("netChange", "NET_CHANGE"), ("netPercentChange", "NET_CHANGE_PERCENT"),
                           ("mark", "MARK"), ("markChange", "MARK_CHANGE"),
                           ("markPercentChange", "MARK_CHANGE_PERCENT"), ("totalVolume", "TOTAL_VOLUME"),
                           ("openPrice", "OPEN_PRICE"), ("highPrice", "HIGH_PRICE"), ("lowPrice", "LOW_PRICE"),
                           ("tradeTime", "TRADE_TIME_MILLIS")):
            if q.get(rest) is not None:
                self._l1[name] = q[rest]
        if self._l1:
            self.quote.emit(new, dict(self._l1))
        await stream.nasdaq_book_subs([new])
        await stream.nyse_book_subs([new])
        await stream.level_one_equity_subs([new])
        self.status.emit(f"Live: waiting for {new}'s book...")
        self.book.emit(new, [], [])

    def _on_level1(self, msg):
        for c in msg.get("content", []):
            if c.get("key") == self._current:
                # After the first message only changed fields come
                self._l1.update({k: v for k, v in c.items() if k.isupper()})
                self.quote.emit(self._current, dict(self._l1))

    def _on_book(self, service, msg):
        for c in msg.get("content", []):
            if c.get("key") != self._current:
                continue            # still arriving for the ticker just left
            self._books[service] = (self._side(c.get("BIDS", []), "BID"), self._side(c.get("ASKS", []), "ASK"))
            # Best price first, then the biggest size, then market makers A-Z, as in thinkorswim
            bids = sorted((r for b, _ in self._books.values() for r in b), key=lambda r: (-r[1], -r[2], r[0]))
            asks = sorted((r for _, a in self._books.values() for r in a), key=lambda r: (r[1], -r[2], r[0]))
            self.book.emit(self._current, self._levels(bids), self._levels(asks))
            n = len(bids) + len(asks)
            self.status.emit(f"Live · {time.strftime('%H:%M:%S')}" if n else
                             f"Live · no quotes for {self._current} now (OTC quotes run 6:00-17:00 ET)")

    @staticmethod
    def _side(levels, side):
        """[(market maker, price, size)] from one side of a book message."""
        rows = []
        for lv in levels:
            price = lv.get(f"{side}_PRICE")
            for p in lv.get(f"{side}S", []):
                rows.append((p.get("EXCHANGE", "?"), price, p.get(f"{side}_VOLUME", 0)))
        return rows

    @staticmethod
    def _levels(rows):
        """Adds the price level number, 0 for the best price."""
        out, level, last = [], -1, None
        for mm, price, size in rows:
            if price != last:
                level, last = level + 1, price
            out.append((mm, price, size, level))
        return out


class Level2Panel(QWidget):
    live_quote = pyqtSignal(str, dict)
    book_changed = pyqtSignal(str, list, list)    # ticker, bids, asks, as shown     # ticker, Level 1 fields: for the chart's price and volume tags

    """The Level II box under the chart, styled after thinkorswim: a quote bar, then bids and asks
    side by side on black, one row per market maker, darker blue for each price level further out."""

    def __init__(self, settings: dict, token_path: Path, mm_icons=None, parent=None):
        """mm_icons: {1: yellow sign, 2: red sign} for the market makers in MM_YELLOW / MM_RED."""
        super().__init__(parent)
        self.settings, self.token_path = settings, token_path
        self.mm_icons = mm_icons or {}
        self.ticker = ""
        self.name = ""
        self.feed = None
        self.prev = {"BID": {}, "ASK": {}}     # market maker -> last size, per side
        self.trend = {"BID": {}, "ASK": {}}    # market maker -> +1/-1, the last size change
        self.prev_quote = {}                   # bid/ask price and size at the last quote update
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
        self.status_label = QLabel()
        self.status_label.setObjectName("l2status")
        head.addWidget(self.status_label)
        root.addWidget(bar)

        sides = QHBoxLayout()
        sides.setContentsMargins(0, 0, 0, 0)
        sides.setSpacing(6)
        self.bid_table = self._table(["Ex", "Bid", "BS"])
        self.ask_table = self._table(["", "Ex", "Ask", "AS"], sign_col=True)
        sides.addWidget(self.bid_table)
        sides.addWidget(self.ask_table)
        root.addLayout(sides, 1)
        self.show_quote({})

    @staticmethod
    def _table(titles, sign_col=False):
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
            t.horizontalHeaderItem(0).setToolTip("Red: a Dilutive Market Maker. Yellow: might sometimes be a Dilutive Market Maker.")
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
            self.bid_table.setRowCount(0)
            self.ask_table.setRowCount(0)
            self.ticker, self.name = ticker, name
            self.show_quote({})
        if not ticker:
            if self.feed:
                self.feed.set_symbol(None)
            return
        key, secret, _ = self.keys()
        if not key or not secret:
            self.set_status("Add schwab_app_key and schwab_app_secret to settings.json to see Level II.")
            return
        if self.feed is None:
            self.feed = Level2Feed(key, secret, self.token_path)
            self.feed.book.connect(self.show_book)
            self.feed.quote.connect(self.on_quote)
            self.feed.status.connect(self.set_status)
            self.feed.login_needed.connect(self.on_login_needed)
        self.feed.set_symbol(ticker)

    def on_quote(self, ticker, q):
        if ticker == self.ticker:
            self.show_quote(q)
            self.live_quote.emit(ticker, q)

    def stop(self):
        if self.feed:
            self.feed.stop()

    def set_status(self, text):
        self.status_label.setText(f"Level II · {text}")

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
        try:
            client_from_received_url(key, secret, ctx, url, write_token(self.token_path))
        except Exception as e:
            QMessageBox.warning(self, "Schwab login failed",
                                f"{e}\n\n\"invalid_grant\" usually means the address was pasted too late. Try again.")
            return
        self.login_btn.hide()
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
        # Bid and ask price and size, each on its own: green when it went up at this update, red when
        # it went down, yellow when it stayed
        def tone(field):
            now, before = q.get(field), self.prev_quote.get(field)
            if now is None or before is None or now == before:
                return LAST
            return GREEN if now > before else RED
        for key, side in (("bid", "BID"), ("ask", "ASK")):
            price = q.get(f"{side}_PRICE")
            size = q.get(f"{side}_SIZE") or 0
            c[key].setText(f"{side.title()}: <span style='color:{tone(side + '_PRICE')}'>{tos_price(price)}</span> x "
                           f"<span style='color:{tone(side + '_SIZE')}'>{size:,}</span>" if price is not None else "")
        self.prev_quote = {f: q[f] for f in ("BID_PRICE", "BID_SIZE", "ASK_PRICE", "ASK_SIZE") if f in q}
        for key, lb in c.items():
            lb.setVisible(bool(lb.text()))
            self.dividers[key].setVisible(bool(lb.text()))

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
        if ticker != self.ticker:
            return
        self._fill(self.bid_table, bids, "BID")
        self._fill(self.ask_table, asks, "ASK")
        self.book_changed.emit(ticker, bids, asks)

    def _fill(self, table, rows, side):
        prev, trend = self.prev[side], self.trend[side]
        table.setRowCount(len(rows))
        for r, (mm, price, size, level) in enumerate(rows):
            # Size colored by its last change, as in thinkorswim: green up, red down
            if mm in prev and size != prev[mm]:
                trend[mm] = 1 if size > prev[mm] else -1
            prev[mm] = size
            bg = QColor(LEVEL_COLORS[min(level, len(LEVEL_COLORS) - 1)])
            cells = [QTableWidgetItem(mm), QTableWidgetItem(tos_price(price)), QTableWidgetItem(f"{size:,}")]
            first = 0
            if side == "ASK":
                # The sign column: red for a diluter, yellow for a so-so market maker
                risk = 2 if mm in MM_RED else 1 if mm in MM_YELLOW else 0
                sign = QTableWidgetItem()
                if risk in self.mm_icons:
                    sign.setIcon(self.mm_icons[risk])
                    sign.setToolTip(MM_TIPS[risk])
                cells.insert(0, sign)
                first = 1
            for col, item in enumerate(cells):
                item.setBackground(bg)
                item.setForeground(QColor(GOLD))
                if col > first:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, col, item)
            if trend.get(mm):
                cells[-1].setForeground(QColor(GREEN if trend[mm] > 0 else RED))
