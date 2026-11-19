"""
otc_news_panel.py - The Chart tab's News panel (in Level II's place, a tab on the left switches them): the
chart's ticker's news from the news channel's history (news.db, otc_news_db), months of it.

  top      the months in a line ("6 months: dilution 24 times (outstanding +534.2%), 1 filing"), the
           filters (All / Warnings / Dilution / Filings / Tier / Headlines), the range (1M / 3M / 6M), and
           the OTC Markets news page
  left     the news, newest first, a line each (a warning in red)
  right    the selected post as the channel wrote it (the old values red, the new green), and under it the
           outstanding shares over the range: a step at each dilution
"""

import html
from datetime import datetime, timedelta

from PyQt6.QtCore import QPointF, QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                             QStyledItemDelegate, QToolTip,
                             QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

import otc_news_db

WARN, INFO, DIM = "#ff6060", "#d8d8d8", "#8a8a8a"
GOLD = "#e3b23c"
KINDS = {"dilution": "dilution", "shares": "shares down", "filing": "filing", "tier_down": "tier down",
         "tier_up": "tier up", "grace": "grace period", "headline": "headline"}
FILTERS = [("All", None), ("⚠️ Warnings", "warn"), ("Dilution", {"dilution"}), ("Filings", {"filing"}),
           ("Tier", {"tier_down", "tier_up", "grace"}), ("Headlines", {"headline"})]
RANGES = [("1M", 30), ("3M", 90), ("6M", 180)]


def short(n):
    n = abs(n or 0)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.2f}".rstrip("0").rstrip(".") + unit
    return f"{n:,.0f}"


def line_of(d, prefer=None):
    """A news' line in the list: (kind, text): the kind filtered for (prefer: a set of kinds), else its
    warning, else its first."""
    flags = d.get("flags") or []
    f = (next((f for f in flags if prefer and f.get("kind") in prefer), None)
         or next((f for f in flags if f.get("level") == "warn"), flags[0] if flags else {}))
    kind = KINDS.get(f.get("kind"), f.get("kind", ""))
    text = f.get("text") or (d.get("summary") or "").splitlines()[0]
    if f.get("kind") == "headline":
        text = text.split(": ", 1)[-1]
    return kind, text


def post_html(d):
    """The post as the channel wrote it: a field's name bold, the old value red, the new green."""
    when = str(d.get("time", "")).replace("T", " ")[:16]
    lines = [f"<span style='color:{DIM}'>{html.escape(when)}</span>"]
    for raw in (d.get("text") or "").splitlines():
        s = html.escape(raw)
        if raw.startswith("❌"):
            lines.append(f"<span style='color:#ff6060'>{s}</span>")
        elif raw.startswith("✅"):
            lines.append(f"<span style='color:#3ccf3c'>{s}</span>")
        elif raw.rstrip().endswith("Changed:") or raw.startswith("Ticker:"):
            lines.append(f"<b>{s}</b>")
        else:
            lines.append(s)
    summary = html.escape(d.get("summary") or "").replace("\n", "<br>")
    return ("<div style='font-size:10pt'>" + "<br>".join(lines) + "</div>"
            + (f"<hr><div style='color:{GOLD}'>{summary}</div>" if summary else ""))


class KeepColorDelegate(QStyledItemDelegate):
    """A selected row keeps its cells' own colors (a warning stays red), on the selection's background."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        if fg is not None:
            color = fg.color() if isinstance(fg, QBrush) else QColor(fg)
            option.palette.setColor(QPalette.ColorRole.HighlightedText, color)


class SharesChart(QWidget):
    """The outstanding shares over the range: a step at each change, with axes (the shares on the left, the
    dates below). Every change marked (a dot on the line and a tick on the time axis: a tiny dilution shows
    too), red up (dilution), green down; hovering a mark tells its date and change."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []            # [(datetime, before, after)], oldest first
        self.start = self.end = None
        self.marks = []             # [(QPointF on the line, x on the axis, its tooltip)] as last drawn
        self.setMinimumHeight(90)
        self.setMouseTracking(True)

    def set_points(self, points, start, end):
        self.points, self.start, self.end = points, start, end
        self.update()

    @staticmethod
    def nice_ticks(lo, hi, n=5):
        """About n round values between lo and hi."""
        import math
        span = hi - lo
        if span <= 0:
            return [lo]
        raw = span / n
        mag = 10 ** math.floor(math.log10(raw))
        step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
        first = math.ceil(lo / step) * step
        out, v = [], first
        while v <= hi + step * 1e-9:
            out.append(v)
            v += step
        return out

    def date_ticks(self):
        """The time axis' marks: weeks for a month or less, else months."""
        out = []
        days = (self.end - self.start).days
        if days <= 35:
            t = (self.start + timedelta(days=(7 - self.start.weekday()) % 7)).replace(hour=0, minute=0, second=0,
                                                                                   microsecond=0)
            while t <= self.end:
                out.append((t, f"{t:%m-%d}"))
                t += timedelta(days=7)
        else:
            t = self.start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while t <= self.end:
                if t >= self.start:
                    out.append((t, f"{t:%b}" if t.month != 1 else f"{t:%b %Y}"))
                t = (t.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        fm = p.fontMetrics()
        p.setPen(QColor(DIM))
        p.drawText(QRectF(self.rect()).adjusted(6, 2, -6, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   "Outstanding shares")
        self.marks = []
        if not self.points or self.start is None:
            p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, "No share changes in this range")
            p.end()
            return
        values = [v for _, a, b in self.points for v in (a, b)]
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.08 or max(hi * 0.01, 1)
        lo, hi = max(0, lo - pad), hi + pad
        ticks = self.nice_ticks(lo, hi)
        left = max(fm.horizontalAdvance(short(v)) for v in ticks) + 10
        r = QRectF(self.rect()).adjusted(left, 18, -10, -(fm.height() + 6))
        span = max((self.end - self.start).total_seconds(), 1)

        def x(t):
            return r.left() + (t - self.start).total_seconds() / span * r.width()

        def y(v):
            return r.bottom() - (v - lo) / (hi - lo) * r.height()
        # the axes: the shares (left), the dates (below), faint lines across
        grid = QColor("#262c33")
        for v in ticks:
            yy = y(v)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(r.left(), yy), QPointF(r.right(), yy))
            p.setPen(QColor(DIM))
            p.drawText(QRectF(0, yy - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       short(v))
        for t, label in self.date_ticks():
            xx = x(t)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(xx, r.top()), QPointF(xx, r.bottom()))
            p.setPen(QColor(DIM))
            p.drawText(QRectF(xx - 40, r.bottom() + 3, 80, fm.height()), Qt.AlignmentFlag.AlignHCenter, label)
        p.setPen(QPen(QColor("#3a4046"), 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.left(), r.bottom()))
        # the line: a step at each change
        path = QPainterPath(QPointF(r.left(), y(self.points[0][1])))
        for t, before, after in self.points:
            path.lineTo(QPointF(x(t), y(before)))
            path.lineTo(QPointF(x(t), y(after)))
        path.lineTo(QPointF(r.right(), y(self.points[-1][2])))
        p.setPen(QPen(QColor("#4fc3f7"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # every change marked: a dot on the line and a tick on the time axis (a tiny one shows too)
        for t, before, after in self.points:
            color = QColor("#ff6060" if after > before else "#3ccf3c")
            pt = QPointF(x(t), y(after))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(pt, 3.5, 3.5)
            p.setPen(QPen(color, 2))
            p.drawLine(QPointF(pt.x(), r.bottom() - 6), QPointF(pt.x(), r.bottom()))
            pct = (after - before) / before * 100 if before else 0
            tip = (f"{t:%Y-%m-%d %H:%M}: {'+' if after >= before else '-'}{abs(after - before):,} "
                   f"({pct:+.2f}%): {short(before)} → {short(after)}")
            self.marks.append((pt, pt.x(), tip))
        first, last = self.points[0][1], self.points[-1][2]
        pct = (last - first) / first * 100 if first else 0
        p.setPen(QColor("#ff6060" if last > first else "#3ccf3c"))
        p.drawText(QRectF(self.rect()).adjusted(6, 2, -10, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                   f"{short(first)} → {short(last)} ({pct:+.1f}%), {len(self.points)} change"
                   f"{'s' if len(self.points) != 1 else ''}")
        p.end()

    def mouseMoveEvent(self, e):
        """A mark's date and change, when hovered (its dot, or its tick on the time axis)."""
        pos = e.position()
        near = [(min((m[0] - pos).manhattanLength(), abs(m[1] - pos.x()) + 0.0), m[2]) for m in self.marks]
        near = [n for n in near if n[0] <= 6]
        if near:
            QToolTip.showText(e.globalPosition().toPoint(), "\n".join(tip for _, tip in sorted(near)[:4]), self)
        else:
            QToolTip.hideText()


class NewsPanel(QWidget):
    """The news of the chart's ticker. news_of(ticker, days): its news, newest first (the app's: the history
    and the newest shared ones); history(ticker, days): its months in a line."""
    count_changed = pyqtSignal(int, bool)       # the last 7 days' news, and whether a warning is among them

    def __init__(self, news_of, history, parent=None):
        super().__init__(parent)
        self.news_of, self.history = news_of, history
        self.ticker = ""
        self.items = []
        self.kind_filter, self.days = None, 180
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        top.addWidget(self.summary, 1)
        self.reading = QLabel("")           # the main server's reading of the channel
        self.reading.setStyleSheet(f"color: {DIM};")
        top.addWidget(self.reading)
        top.addSpacing(8)

        def segment(names, pick, start):
            group = QButtonGroup(self)
            group.setExclusive(True)
            for i, (name, value) in enumerate(names):
                b = QPushButton(name)
                b.setCheckable(True)
                b.setChecked(i == start)
                b.setStyleSheet("QPushButton { padding: 1px 7px; } QPushButton:checked { background: #3a4a5a; "
                                "color: #ffffff; font-weight: bold; }")
                b.clicked.connect(lambda _c=False, v=value: pick(v))
                group.addButton(b)
                top.addWidget(b)
            return group
        self.filters = segment(FILTERS, self.set_filter, 0)
        top.addSpacing(8)
        self.ranges = segment(RANGES, self.set_days, 2)
        top.addSpacing(8)
        web = QPushButton("OTC Markets ↗")
        web.setToolTip("The ticker's news page on otcmarkets.com, in the browser")
        web.clicked.connect(self.open_web)
        top.addWidget(web)
        root.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Date", "Kind", "News"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setStyleSheet("QTableWidget { background: #0d1014; gridline-color: #22282f; "
                                 "border: 1px solid #2a3038; } QTableWidget::item:selected { background: #1a2a3a; }")
        # (the selection: a dark blue; its text keeps each cell's color: KeepColorDelegate)
        self.table.setItemDelegate(KeepColorDelegate(self.table))
        self.table.itemSelectionChanged.connect(self.show_selected)
        split.addWidget(self.table)
        right = QSplitter(Qt.Orientation.Vertical)
        self.post = QTextBrowser()
        self.post.setOpenExternalLinks(True)
        self.post.setStyleSheet("QTextBrowser { background: #0d1014; color: #d8d8d8; border: 1px solid #2a3038; }")
        right.addWidget(self.post)
        self.shares = SharesChart()
        right.addWidget(self.shares)
        right.setSizes([120, 150])
        split.addWidget(right)
        split.setSizes([600, 420])
        root.addWidget(split, 1)
        self.empty = "No news of {t} in the news channel in this range."

    # ---- in ----
    def set_ticker(self, ticker):
        ticker = (ticker or "").upper()
        if ticker != self.ticker:
            self.ticker = ticker
            self.refresh()

    def set_filter(self, value):
        self.kind_filter = value
        self.fill()

    def set_days(self, days):
        self.days = days
        self.refresh()

    def show_reading(self, msg):
        """The main server's reading: "Reading the latest news... 300 posts", then "Up to date, checked
        14:32:05" (the panel shows what came)."""
        at = str(msg.get("at") or "")[11:19]
        if msg.get("reading"):
            n = msg.get("read") or 0
            self.reading.setText("⏳ Reading the latest news..." + (f" {n:,} posts" if n else ""))
            self.reading.setStyleSheet(f"color: {GOLD};")
        else:
            n = msg.get("read") or 0
            self.reading.setText(f"✓ Up to date · checked {at}" + (f" ({n:,} new)" if n else ""))
            self.reading.setStyleSheet(f"color: {DIM};")
            if n:
                self.refresh()

    def open_web(self):
        if self.ticker:
            QDesktopServices.openUrl(QUrl(f"https://www.otcmarkets.com/stock/{self.ticker}/news"))

    def refresh(self):
        """The ticker's news again (a new ticker, the range, news that came)."""
        self.items = self.news_of(self.ticker, self.days) if self.ticker else []
        months = self.history(self.ticker, self.days) if self.ticker else None
        self.summary.setText(f"{self.ticker}: {months}" if months else
                             f"{self.ticker}: no news in {self.days // 30} month{'s' if self.days > 30 else ''}"
                             if self.ticker else "Open a ticker on the chart")
        recent = [d for d in self.items
                  if str(d.get("time", ""))[:19] >= (datetime.now() - timedelta(days=7)).isoformat()[:19]]
        self.count_changed.emit(len(recent), any(d.get("level") == "warn" for d in recent))
        self.fill()
        self.fill_shares()

    # ---- out ----
    def shown_items(self):
        f = self.kind_filter
        if f is None:
            return self.items
        if f == "warn":
            return [d for d in self.items if d.get("level") == "warn"]
        return [d for d in self.items if any(x.get("kind") in f for x in d.get("flags") or [])]

    def fill(self):
        items = self.shown_items()
        self.table.setRowCount(len(items))
        prefer = self.kind_filter if isinstance(self.kind_filter, set) else None
        for row, d in enumerate(items):
            kind, text = line_of(d, prefer)
            warn = d.get("level") == "warn"
            color = QColor(WARN if warn else INFO)
            when = str(d.get("time", "")).replace("T", " ")[:16]          # yyyy-mm-dd HH:MM
            cells = [when, ("⚠️ " if warn else "📰 ") + kind, text]
            for col, value in enumerate(cells):
                item = QTableWidgetItem(value)
                item.setForeground(QBrush(color if col else QColor(DIM)))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                if col == 2:
                    item.setToolTip(d.get("summary") or "")
                self.table.setItem(row, col, item)
        if items:
            self.table.selectRow(0)
            self.show_selected()        # (row 0 selected before too: no selection change to show it)
        else:
            self.post.setHtml(f"<span style='color:{DIM}'>{html.escape(self.empty.format(t=self.ticker or 'it'))}"
                              "</span>")

    def show_selected(self):
        rows = self.table.selectionModel().selectedRows()
        items = self.shown_items()
        if rows and rows[0].row() < len(items):
            self.post.setHtml(post_html(items[rows[0].row()]))

    def fill_shares(self):
        """The outstanding shares' changes in the range (from the posts kept), oldest first."""
        points = []
        for d in sorted(self.items, key=lambda d: str(d.get("time", ""))):
            before, after = otc_news_db.outstanding(d.get("text"))
            if before is not None and after is not None:
                try:
                    t = datetime.fromisoformat(str(d["time"])[:19])
                except ValueError:
                    continue
                points.append((t, before, after))
        end = datetime.now()
        self.shares.set_points(points, end - timedelta(days=self.days), end)
