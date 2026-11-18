"""
otc_probe.py - The ATM scanner's Checks: the Level 1 and Level II of many tickers at once, for the
checklist's checks of each (otc_checklist.scan_checks).

Live: the tickers are asked of this computer's server as probe tickers (otc_server_link.probe), in
batches: the server streams them for a moment, last in priority (only in Schwab's free room: 500 for
Level 1, but only 100 for Level II: a batch is as big as the Level II room),
with one REST quotes call for their Level 1; a ticker is done when its first book comes (Schwab
sends the whole book right after subscribing), else after PROBE_WAIT_MS (no Level II).
Held (outside the market's hours, with "Hold last L1/L2 at end of day" on): each ticker's market at
the last close from the record (the server's at_close), N/A when it wasn't recorded.

The candle part (the bounce range and the volume) is worked out before, from the database
(candle_part), as the chart's checklist does.
"""

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import otc_atm as atm
import otc_zones as zones

PROBE_BATCH = 150           # tickers streamed at once (fewer when Schwab's Level II room is smaller)
PROBE_WAIT_MS = 10000       # a batch's most time: the tickers without a book by then have none
CLOSE_IN_FLIGHT = 20        # at_close questions out at once (each reads the record's files)
CLOSE_WAIT_MS = 30000       # the most time for the answers of the record


def candle_part(con, ticker, first_day, tol, gap):
    """(the chart's candle rows, the bounce range or None), as the chart's checklist has them."""
    rows = con.execute("SELECT date, open, high, low, close, volume, vwap FROM candles "
                       "WHERE ticker = ? ORDER BY date", (ticker,)).fetchall()
    rows = zones.clean_rows(rows)
    rng = None
    good = [r for r in rows if None not in r[1:5]]
    if good and first_day:
        import numpy as np
        dates, data = atm.clean([r[0] for r in good],
                                np.array([[*r[1:5], r[5] or 0.0] for r in good], dtype=float))
        if dates:
            rng = atm.recent_range(dates, data, first_day, tol, gap)
    return rows, rng


class ProbeRunner(QObject):
    """Reads the market of many tickers, live or at the last close. result: (ticker, {"q", "bids",
    "asks", "has_l2", "source": "live" / "close" / "none", "at": ms or None}); progress: (done,
    total, text); finished: (text)."""
    result = pyqtSignal(str, dict)
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str)

    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.room = None            # Schwab's free Level II room, from the server's last "streamed" count
        self.running = False
        self.held = False
        self.todo = []              # tickers not asked yet
        self.batch = set()          # the live batch out now / the at_close questions out now
        self.sent = 0               # tickers in the probe the server has now
        self.q, self.books = {}, {}
        self.done = 0
        self.total = 0
        self.no_l2 = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        link.message.connect(self._on_message)

    # ---- in ----
    def start(self, tickers, held):
        self.stop(quiet=True)
        self.running, self.held = True, held
        self.todo = list(dict.fromkeys(t for t in tickers if t))
        self.total, self.done, self.no_l2 = len(self.todo), 0, 0
        self.q, self.books, self.batch = {}, {}, set()
        if not self.todo:
            self._finish("Nothing to check.")
            return
        if held:
            self.timer.start(CLOSE_WAIT_MS)
            self._ask_closes()
        else:
            self._next_batch()

    def stop(self, quiet=False):
        if not self.running:
            return
        self.running = False
        self.timer.stop()
        if not self.held:
            self._probe([])
        self.batch, self.todo = set(), []
        if not quiet:
            self.finished.emit(f"Stopped: {self.done:,} of {self.total:,} checked.")

    # ---- live ----
    def _probe(self, tickers):
        self.sent = len(tickers)
        self.link.probe(tickers)

    def _next_batch(self):
        if not self.todo:
            self._probe([])
            self._finish(f"Checked {self.total:,} tickers live"
                         + (f" ({self.no_l2:,} without Level II)." if self.no_l2 else "."))
            return
        size = PROBE_BATCH if self.room is None else max(0, min(PROBE_BATCH, self.room))
        if size < 1:
            self._probe([])
            self._finish("Schwab's Level II (100 tickers) is all in use by the charts, the trades and the "
                         "favorites: no room to check more now.")
            return
        part, self.todo = self.todo[:size], self.todo[size:]
        self.batch = set(part)
        for t in part:
            self.q[t] = {}
        self._probe(part)
        self.timer.start(PROBE_WAIT_MS)
        self._progress()

    def _on_message(self, msg):
        kind = msg.get("type")
        if kind == "streamed":
            # the Level II room besides this probe's own tickers (counted in "wanted" while out): the
            # books go to the first book_max wanted, the probe's last
            out = self.sent if self.running and not self.held else 0
            most = int(msg.get("book_max") or msg.get("max", 500))
            self.room = most - max(0, int(msg.get("wanted", 0)) - out)
            return
        if not self.running:
            return
        t = msg.get("ticker")
        if t not in self.batch:
            return
        if kind == "quote" and not self.held:
            self.q[t] = dict(msg.get("q") or {})
        elif kind == "book" and not self.held and msg.get("got"):
            self.books[t] = ([tuple(r) for r in msg.get("bids") or []], [tuple(r) for r in msg.get("asks") or []])
            self.batch.discard(t)
            self._emit_live(t, True)
            if not self.batch:
                self.timer.stop()
                self._next_batch()
        elif kind == "at_close" and self.held:
            self.batch.discard(t)
            if msg.get("q") is None:
                self.result.emit(t, {"q": {}, "bids": [], "asks": [], "has_l2": False, "source": "none",
                                     "at": None})
            else:
                self.result.emit(t, {"q": dict(msg["q"]), "has_l2": True, "source": "close",
                                     "bids": [tuple(r) for r in msg.get("bids") or []],
                                     "asks": [tuple(r) for r in msg.get("asks") or []],
                                     "at": msg.get("close")})
            self.done += 1
            self._ask_closes()

    def _emit_live(self, t, has_l2):
        bids, asks = self.books.get(t, ([], []))
        self.result.emit(t, {"q": self.q.get(t, {}), "bids": bids, "asks": asks, "has_l2": has_l2,
                             "source": "live" if has_l2 or self.q.get(t) else "none", "at": None})
        self.done += 1
        self.no_l2 += not has_l2
        self._progress()

    def _timeout(self):
        if not self.running:
            return
        if self.held:
            for t in sorted(self.batch) + self.todo:
                self.result.emit(t, {"q": {}, "bids": [], "asks": [], "has_l2": False, "source": "none", "at": None})
            self.done = self.total
            self.batch, self.todo = set(), []
            self._finish("The record didn't answer for some tickers (N/A).")
            return
        for t in sorted(self.batch):
            self._emit_live(t, False)
        self.batch = set()
        self._next_batch()

    # ---- held: the record ----
    def _ask_closes(self):
        while self.todo and len(self.batch) < CLOSE_IN_FLIGHT:
            t = self.todo.pop(0)
            self.batch.add(t)
            if not self.link.send({"type": "at_close", "ticker": t}):
                self.batch.discard(t)
                self.result.emit(t, {"q": {}, "bids": [], "asks": [], "has_l2": False, "source": "none", "at": None})
                self.done += 1
        self._progress()
        if not self.todo and not self.batch:
            self.timer.stop()
            self._finish(f"Checked {self.total:,} tickers at the last close.")

    def _progress(self):
        how = "at the last close" if self.held else "live"
        self.progress.emit(self.done, self.total, f"Checking L1/L2 {how}: {self.done:,} of {self.total:,}...")

    def _finish(self, text):
        self.running = False
        self.timer.stop()
        self.finished.emit(text)
