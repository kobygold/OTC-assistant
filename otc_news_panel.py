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
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QPushButton,
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


class SharesChart(QWidget):
    """The outstanding shares over the range: a step at each change (a dilution goes up, red)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []            # [(datetime, before, after)], oldest first
        self.start = self.end = None
        self.setMinimumHeight(80)

    def set_points(self, points, start, end):
        self.points, self.start, self.end = points, start, end
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(8, 18, -8, -18)
        p.setPen(QColor(DIM))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.drawText(QRectF(self.rect()).adjusted(8, 2, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   "Outstanding shares")
        if not self.points or self.start is None:
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "No share changes in this range")
            p.end()
            return
        values = [v for _, a, b in self.points for v in (a, b)]
        lo, hi = min(values), max(values)
        if hi == lo:
            hi = lo + 1
        span = max((self.end - self.start).total_seconds(), 1)

        def x(t):
            return r.left() + (t - self.start).total_seconds() / span * r.width()

        def y(v):
            return r.bottom() - (v - lo) / (hi - lo) * r.height()
        path = QPainterPath(QPointF(r.left(), y(self.points[0][1])))
        for t, before, after in self.points:
            path.lineTo(QPointF(x(t), y(before)))
            path.lineTo(QPointF(x(t), y(after)))
        path.lineTo(QPointF(r.right(), y(self.points[-1][2])))
        p.setPen(QPen(QColor("#4fc3f7"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for t, before, after in self.points:         # the steps: up (dilution) red, down green
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ff6060" if after > before else "#3ccf3c"))
            p.drawEllipse(QPointF(x(t), y(after)), 3, 3)
        first, last = self.points[0][1], self.points[-1][2]
        pct = (last - first) / first * 100 if first else 0
        p.setPen(QColor(INFO))
        p.drawText(QRectF(r.left(), r.bottom() + 2, r.width() / 2, 14), Qt.AlignmentFlag.AlignLeft,
                   f"{short(first)}  ({self.start:%m-%d})")
        p.setPen(QColor("#ff6060" if last > first else "#3ccf3c"))
        p.drawText(QRectF(r.left() + r.width() / 2, r.bottom() + 2, r.width() / 2, 14), Qt.AlignmentFlag.AlignRight,
                   f"{short(last)}  ({pct:+.1f}%)")
        p.end()


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
                                 "border: 1px solid #2a3038; } QTableWidget::item:selected { background: #2a3a4c; }")
        self.table.itemSelectionChanged.connect(self.show_selected)
        split.addWidget(self.table)
        right = QSplitter(Qt.Orientation.Vertical)
        self.post = QTextBrowser()
        self.post.setOpenExternalLinks(True)
        self.post.setStyleSheet("QTextBrowser { background: #0d1014; color: #d8d8d8; border: 1px solid #2a3038; }")
        right.addWidget(self.post)
        self.shares = SharesChart()
        right.addWidget(self.shares)
        right.setSizes([140, 100])
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
            when = str(d.get("time", "")).replace("T", " ")[2:16]
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
