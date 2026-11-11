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

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from otc_chart import fmt_price

TOKEN_DAYS = 7                  # Schwab's refresh token runs out after this
BOOK_SERVICES = ("NASDAQ_BOOK", "NYSE_BOOK")
RECONNECT_SECONDS = 15
POLL_SECONDS = 0.3              # how often the stream loop checks for a new ticker

# One color per price level, best first, as in thinkorswim; the same on both sides
LEVEL_COLORS = ["#fff1a8", "#cfe6ff", "#d6f2d0", "#ffd9d2", "#e6dcff", "#d4f0ef", "#f6e0f0", "#ececec"]
UP_COLOR = QColor("#0a8a2a")
DOWN_COLOR = QColor("#c62020")


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
                           ("totalVolume", "TOTAL_VOLUME")):
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
            bids = sorted((r for b, _ in self._books.values() for r in b), key=lambda r: (-r[1], -r[2]))
            asks = sorted((r for _, a in self._books.values() for r in a), key=lambda r: (r[1], -r[2]))
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
    """The Level II box under the chart: a quote line, then bids and asks side by side."""

    def __init__(self, settings: dict, token_path: Path, parent=None):
        super().__init__(parent)
        self.settings, self.token_path = settings, token_path
        self.ticker = ""
        self.name = ""
        self.feed = None
        self.prev = {"BID": {}, "ASK": {}}     # market maker -> last size, per side
        self.trend = {"BID": {}, "ASK": {}}    # market maker -> +1/-1, the last size change

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        head = QHBoxLayout()
        self.head_label = QLabel()
        self.head_label.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(self.head_label)
        head.addStretch()
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #666;")
        head.addWidget(self.status_label)
        self.login_btn = QPushButton("Log in to Schwab...")
        self.login_btn.clicked.connect(self.login)
        self.login_btn.hide()
        head.addWidget(self.login_btn)
        root.addLayout(head)

        sides = QHBoxLayout()
        sides.setSpacing(6)
        self.bid_table = self._table(["MM", "Bid", "Size"])
        self.ask_table = self._table(["MM", "Ask", "Size"])
        sides.addWidget(self.bid_table)
        sides.addWidget(self.ask_table)
        root.addLayout(sides, 1)
        self.show_quote({})

    @staticmethod
    def _table(titles):
        t = QTableWidget(0, len(titles))
        t.setHorizontalHeaderLabels(titles)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(t.fontMetrics().height() + 4)
        t.setShowGrid(False)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.horizontalHeaderItem(0).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col in (1, 2):
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
            self.feed.quote.connect(lambda t, q: t == self.ticker and self.show_quote(q))
            self.feed.status.connect(self.set_status)
            self.feed.login_needed.connect(self.on_login_needed)
        self.feed.set_symbol(ticker)

    def stop(self):
        if self.feed:
            self.feed.stop()

    def set_status(self, text):
        self.status_label.setText(text)

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
        last, close = q.get("LAST_PRICE"), q.get("CLOSE_PRICE")
        change = q.get("NET_CHANGE")
        if change is None and last is not None and close:
            change = last - close
        pct = change / close * 100 if change is not None and close else None
        color = "#0a8a2a" if (change or 0) > 0 else "#c62020" if (change or 0) < 0 else "#444"
        parts = [f"<b>{self.ticker or 'Level II'}</b>"]
        if self.name:
            parts.append(self.name[:40])
        if last is not None:
            parts.append(f"<b style='font-size:13pt'>{fmt_price(last)}</b>")
        if change is not None:
            parts.append(f"<span style='color:{color}'>{change:+.6f}".rstrip("0").rstrip(".")
                         + (f" ({pct:+.2f}%)" if pct is not None else "") + "</span>")
        if q.get("BID_PRICE") is not None:
            parts.append(f"Bid: <b>{fmt_price(q['BID_PRICE'])}</b> x {q.get('BID_SIZE', 0):,}")
        if q.get("ASK_PRICE") is not None:
            parts.append(f"Ask: <b>{fmt_price(q['ASK_PRICE'])}</b> x {q.get('ASK_SIZE', 0):,}")
        if q.get("TOTAL_VOLUME") is not None:
            parts.append(f"Volume: {q['TOTAL_VOLUME']:,}")
        self.head_label.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def show_book(self, ticker, bids, asks):
        if ticker != self.ticker:
            return
        self._fill(self.bid_table, bids, "BID")
        self._fill(self.ask_table, asks, "ASK")

    def _fill(self, table, rows, side):
        prev, trend = self.prev[side], self.trend[side]
        table.setRowCount(len(rows))
        bold = QFont(table.font())
        bold.setBold(True)
        for r, (mm, price, size, level) in enumerate(rows):
            # Size colored by its last change, as in thinkorswim: green up, red down
            if mm in prev and size != prev[mm]:
                trend[mm] = 1 if size > prev[mm] else -1
            prev[mm] = size
            bg = QColor(LEVEL_COLORS[min(level, len(LEVEL_COLORS) - 1)])
            cells = [QTableWidgetItem(mm), QTableWidgetItem(fmt_price(price)), QTableWidgetItem(f"{size:,}")]
            for c, item in enumerate(cells):
                item.setBackground(bg)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if level == 0:
                    item.setFont(bold)
                table.setItem(r, c, item)
            if trend.get(mm):
                cells[2].setForeground(UP_COLOR if trend[mm] > 0 else DOWN_COLOR)
