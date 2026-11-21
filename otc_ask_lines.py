"""
otc_ask_lines.py - The Ask side of Level II over the 1-minute chart (the 1D view): each market maker's ask
price, minute by minute, from the record (market_log, otc_recorder: the tickers streamed - the favorites,
the ATM trades, the chart's), as a line of its own color, with a legend on the chart.

ask_events(folder, ticker, day): [(millis, {market maker: its lowest ask price})], one each time the asks
changed (the record's "L2" lines; the books of each service merged).
ask_slots(events, times): the asks at the end of each candle's minute (the 1D view's Israel times).
AskLinesLoader: reads them in a thread (the days before today read once).
"""

import gzip
import json
import threading
from datetime import timedelta
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from otc_intraday import israel_time

# distinct colors on black, none of them the candles' green / red first
PALETTE = ["#4fc3f7", "#ff9800", "#e040fb", "#ffeb3b", "#00e5ff", "#ff80ab", "#b2ff59", "#7c4dff",
           "#ffab40", "#18ffff", "#f06292", "#c6ff00", "#b388ff", "#ffd180", "#64ffda", "#ff5252",
           "#80d8ff", "#eeff41", "#ea80fc", "#a7ffeb"]


def ask_events(folder, ticker, day):
    """The ticker's asks through a market day (YYYY-MM-DD) from the record: [(millis, {mm: lowest ask})],
    oldest first, only when they changed."""
    base = Path(folder) / day / f"{ticker}.jsonl"
    lines = []
    for path, opener in ((base.with_suffix(".jsonl.gz"), gzip.open), (base, open)):
        if not path.exists():
            continue
        try:
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    if '"L2"' not in line:
                        continue
                    try:
                        x = json.loads(line)
                    except ValueError:
                        continue            # (a line being written)
                    if x.get("k") == "L2":
                        lines.append(x)
        except OSError:
            continue
    lines.sort(key=lambda x: x.get("t", 0))
    books = {}          # service -> {mm: price}
    out, last = [], None
    for x in lines:
        d = x.get("d") or {}
        asks = {}
        for level in d.get("a") or []:
            try:
                price, makers = float(level[0]), level[1]
            except (TypeError, ValueError, IndexError):
                continue
            for m in makers or []:
                if m and m[0] and (m[0] not in asks or price < asks[m[0]]):
                    asks[m[0]] = price
        books[d.get("s", "")] = asks
        merged = {}
        for book in books.values():
            for mm, price in book.items():
                if mm not in merged or price < merged[mm]:
                    merged[mm] = price
        if merged != last:
            out.append((int(x.get("t", 0)), merged))
            last = merged
    return out


def ask_slots(events, times):
    """{mm: price} for each candle (times: its minute's Israel time, oldest first): the asks at the end of its
    minute (the last change before the next minute starts); None before the record has any."""
    stamped = [(israel_time(t), asks) for t, asks in events]
    out, j, now = [], 0, None
    for start in times:
        end = start + timedelta(minutes=1)
        while j < len(stamped) and stamped[j][0] < end:
            now = stamped[j][1]
            j += 1
        out.append(now)
    return out


def colors_for(slots, known=None):
    """{mm: color}: the ones known keep theirs; new ones the next colors (by name), so a color doesn't move."""
    colors = dict(known or {})
    names = sorted({mm for s in slots if s for mm in s} - set(colors))
    for mm in names:
        colors[mm] = PALETTE[len(colors) % len(PALETTE)]
    return colors


class AskLinesLoader(QObject):
    """done(ticker, times, slots) in the GUI thread: the asks of each of the 1D view's candles."""
    done = pyqtSignal(str, list, list)

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.cache = {}         # (ticker, day) -> events, for the days before the newest one (they don't change)
        self.lock = threading.Lock()

    def request(self, ticker, times, days):
        """days: the market days of the candles (YYYY-MM-DD), oldest first."""
        threading.Thread(target=self._run, args=(ticker, list(times), list(days)), daemon=True).start()

    def _run(self, ticker, times, days):
        try:
            events = []
            for n, day in enumerate(days):
                with self.lock:
                    cached = self.cache.get((ticker, day))
                if cached is None:
                    cached = ask_events(self.folder, ticker, day)
                    if n < len(days) - 1:
                        with self.lock:
                            self.cache[(ticker, day)] = cached
                events += cached
            self.done.emit(ticker, times, ask_slots(events, times))
        except Exception:
            self.done.emit(ticker, times, [])
